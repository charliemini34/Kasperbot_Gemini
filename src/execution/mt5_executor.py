
# Fichier: src/execution/mt5_executor.py
# Version: 20.0.1 (Hotfix-Circular-Import)
# Dépendances: MetaTrader5, pandas, logging, math, re, src.journal.professional_journal, decimal
# Description: Supprime l'importation circulaire fatale (ImportError).

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
import math
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from src.constants import BUY, SELL
from src.journal.professional_journal import ProfessionalJournal

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.risk.risk_manager import RiskManager

class MT5Executor:
    def __init__(self, mt5_connection, config: dict):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'
        self._trade_context = {}
        self.professional_journal = ProfessionalJournal(config)
        self.config = config

    def get_mt5_connection(self):
        """Retourne l'objet de connexion MetaTrader5."""
        return self._mt5

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        try:
            positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
            if positions is None:
                self.log.warning(f"Impossible de récupérer les positions: {self._mt5.last_error()}")
                return []
            return [pos for pos in positions if magic == 0 or pos.magic == magic]
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération des positions: {e}", exc_info=True)
            return []

    def get_total_floating_pl(self, magic_number: int = 0) -> float:
        """Calcule la somme des profits/pertes flottants pour les positions ouvertes."""
        try:
            open_positions = self.get_open_positions(magic=magic_number)
            if not open_positions:
                return 0.0
            total_floating_pl = sum(pos.profit for pos in open_positions)
            return total_floating_pl
        except Exception as e:
            self.log.error(f"Erreur lors du calcul du PnL flottant: {e}", exc_info=True)
            return 0.0

    def execute_trade(self, account_info, risk_manager: 'RiskManager', symbol: str, direction: str,
                        volume: float, sl: float, tp: float, pattern_name: str, magic_number: int):
        self.log.info(f"--- DÉBUT DE L'EXÉCUTION DU TRADE POUR {symbol} ---")
        price_info = self._mt5.symbol_info_tick(symbol)
        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return

        price = price_info.ask if direction == BUY else price_info.bid

        if volume > 0:
            self.log.info(f"Paramètres de l'ordre: {direction} {volume:.4f} lot(s) de {symbol} @ {price:.5f}, SL={sl:.5f}, TP={tp:.5f}")
            trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
            self.log.debug(f"DEBUG VOLUME FINAL pour {symbol}: Tentative d'envoi avec volume = {volume}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)

            if result and result.order > 0:
                ohlc_data_for_atr = self._mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 100)
                atr_value = 0
                if ohlc_data_for_atr is not None:
                     df_atr = pd.DataFrame(ohlc_data_for_atr)
                     if not df_atr.empty:
                        if hasattr(risk_manager, '_calculate_atr'):
                            atr_value = risk_manager._calculate_atr(df_atr, 14) or 0
                        else:
                            self.log.warning("RiskManager n'a pas la méthode _calculate_atr.")

                partial_tp_levels = self.config.get('risk_management', {}).get('partial_tp', {}).get('levels', [])
                num_partial_levels = len(partial_tp_levels)
                
                self._trade_context[result.order] = {
                    'ticket': result.order, 'symbol': symbol, 'direction': direction,
                    'open_time': datetime.utcnow().isoformat(), 'pattern_trigger': pattern_name,
                    'initial_volume': volume, 'remaining_volume': volume,
                    'partial_tp_state': [False] * num_partial_levels, 
                    'sl_initial': sl,
                    'volatility_atr': atr_value,
                    'be_applied': False, 
                    'be_triggered_by_ptp1': False 
                }
                self.log.debug(f"Contexte créé pour trade #{result.order}: {self._trade_context[result.order]}")
        else:
            self.log.warning(f"Execute_trade appelé avec volume 0 pour {symbol}.")


    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        sanitized_pattern = re.sub(r'[^a-zA-Z0-9_]', '', pattern_name.replace(" ", "_"))
        comment = f"KasperBot-{sanitized_pattern}"[:31]
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": order_type, "price": float(price), "sl": float(sl), "tp": float(tp),
            "deviation": 20, "magic": magic_number, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        self.log.debug(f"Envoi requête ordre: {request}")
        try: result = self._mt5.order_send(request)
        except Exception as e: self.log.critical(f"Exception envoi ordre : {e}", exc_info=True); return None
        if result is None: self.log.error(f"Échec critique envoi order_send=None. Erreur MT5: {self._mt5.last_error()}"); return None
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Ordre placé OK: Ticket #{result.order}, Retcode: {result.retcode}")
            return result
        else:
            self.log.error(f"Échec envoi ordre: retcode={result.retcode}, commentaire={result.comment}")
            if result.retcode == 10014:
                 symbol_info_debug = self._mt5.symbol_info(symbol)
                 if symbol_info_debug: self.log.error(f"DEBUG VOLUME {symbol}: Reçu 10014. Vol={volume}. Broker: Min={symbol_info_debug.volume_min}, Max={symbol_info_debug.volume_max}, Step={symbol_info_debug.volume_step}")
                 else: self.log.error(f"DEBUG VOLUME {symbol}: Reçu 10014. Vol={volume}. Infos symbole indisponibles.")
            return None

    def close_partial_position(self, position, volume_to_close: float) -> bool:
        if volume_to_close <= 0: return False
        symbol_info = self._mt5.symbol_info(position.symbol)
        if not symbol_info: return False
        volume_step = symbol_info.volume_step
        if volume_step > 0:
            vol_to_close_d = Decimal(str(volume_to_close))
            vol_step_d = Decimal(str(volume_step))
            num_steps = (vol_to_close_d / vol_step_d).to_integral_value(rounding=ROUND_DOWN)
            volume_to_close = float(num_steps * vol_step_d)
        vol_digits = int(abs(Decimal(str(volume_step)).log10())) if vol_step > 0 else 2
        volume_to_close = round(min(volume_to_close, position.volume), vol_digits)
        if volume_to_close < symbol_info.volume_min and volume_to_close > 0: return False
        if volume_to_close <= 0: return False
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price_info = self._mt5.symbol_info_tick(position.symbol)
        if not price_info: return False
        price = price_info.bid if order_type == mt5.ORDER_TYPE_SELL else price_info.ask
        
        comment = f"Partial_TP_{volume_to_close:.{vol_digits}f}_lots"

        request = {
            "action": mt5.TRADE_ACTION_DEAL, "position": position.ticket, "symbol": position.symbol,
            "volume": volume_to_close, "type": order_type, "price": price, "deviation": 20,
            "magic": position.magic, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        self.log.info(f"Tentative clôture partielle {volume_to_close:.{vol_digits}f} lots #{position.ticket}...")
        self.log.debug(f"Requête clôture partielle: {request}")
        try: result = self._mt5.order_send(request)
        except Exception as e: self.log.critical(f"Exception clôture partielle #{position.ticket} : {e}", exc_info=True); return False
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Clôture partielle {volume_to_close:.{vol_digits}f} lots #{position.ticket} OK. Ordre: #{result.order}")
            return True
        else:
            retcode = result.retcode if result else "None"; comment = result.comment if result else self._mt5.last_error()
            self.log.error(f"Échec clôture partielle #{position.ticket}: retcode={retcode}, commentaire={comment}")
            return False

    def check_for_closed_trades(self, magic_number: int):
        try:
            from_date = datetime.utcnow() - timedelta(days=7)
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())
            if history_deals is None: return
            current_context_tickets = list(self._trade_context.keys())
            mt5_open_positions = self._mt5.positions_get(magic=magic_number)
            mt5_open_tickets = {pos.ticket for pos in mt5_open_positions} if mt5_open_positions else set()
            for ticket in current_context_tickets:
                if ticket not in mt5_open_tickets:
                    context = self._trade_context.pop(ticket)
                    exit_deals = [d for d in history_deals if d.position_id == ticket and d.entry == 1]
                    if exit_deals:
                         last_exit_deal = max(exit_deals, key=lambda d: d.time)
                         total_pnl = sum(d.profit for d in exit_deals)
                         trade_record = {
                            'ticket': ticket, 'symbol': context['symbol'], 'direction': context['direction'],
                            'open_time': context['open_time'],
                            'close_time': datetime.fromtimestamp(last_exit_deal.time).isoformat(),
                            'pnl': total_pnl,
                            'pattern_trigger': context['pattern_trigger'],
                            'volatility_atr': context.get('volatility_atr', 0)
                         }
                         self._archive_trade(trade_record)
                         self.professional_journal.record_trade(trade_record, self.get_account_info())
                    else:
                         self.log.warning(f"Trade #{ticket} clôturé mais deal sortie introuvable.")
        except Exception as e:
            self.log.error(f"Erreur vérification trades fermés: {e}", exc_info=True)


    def _archive_trade(self, trade_record: dict):
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
            self.log.info(f"Trade #{trade_record['ticket']} (clôture finale) archivé avec PnL total {trade_record['pnl']:.2f}$.")
        except IOError as e:
            self.log.error(f"Erreur archivage trade #{trade_record['ticket']}: {e}")

    def get_account_info(self):
        try: return self._mt5.account_info()
        except Exception as e:
            self.log.error(f"Erreur récupération infos compte: {e}")
            return None

    def modify_position_sl_tp(self, ticket, sl, tp):
        positions = self._mt5.positions_get(ticket=ticket)
        if not positions:
            self.log.warning(f"Tentative de modifier la position #{ticket} qui n'existe plus.")
            return

        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": float(sl), "tp": float(tp)}
        
        result = self._mt5.order_send(request)
        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_comment = result.comment if result else "Résultat vide"
            self.log.error(f"Échec modification pos #{ticket}: {error_comment}")
        else:
            self.log.info(f"Position #{ticket} modifiée (SL: {sl}, TP: {tp}).")

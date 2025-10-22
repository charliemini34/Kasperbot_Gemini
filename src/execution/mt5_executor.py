# Fichier: src/execution/mt5_executor.py
# Version: 1.0.0
# Dépendances: MetaTrader5, pandas, logging, os, datetime, timedelta, src.constants, src.journal.professional_journal
# Description: Version initiale avant modifications v1.0.1+.

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
from datetime import datetime, timedelta
import pytz # Importé mais potentiellement non utilisé en v1.0.0

from src.constants import BUY, SELL
# Assumer que ProfessionalJournal existe et est importable
from src.journal.professional_journal import ProfessionalJournal

class MT5Executor:
    # --- Version 1.0.0: Prend la connexion brute ---
    def __init__(self, mt5_connection, config: dict):
        self._mt5 = mt5_connection # Stocke la connexion brute
        # --- Fin Version ---
        self.log = logging.getLogger(self.__class__.__name__)
        self._config = config
        self.history_file = 'trade_history.csv'
        self._trade_context = {}
        self.professional_journal = ProfessionalJournal(config)

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        """Récupère les positions ouvertes."""
        # --- Version 1.0.0: Appel direct ---
        try:
            positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
            if positions is None:
                self.log.warning(f"Impossible récupérer positions: {self._mt5.last_error()}")
                return []
            return [pos for pos in positions if magic == 0 or pos.magic == magic]
        except Exception as e:
            self.log.error(f"Erreur get_open_positions: {e}", exc_info=True)
            return []
        # --- Fin Version ---

    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number, trade_signal: dict):
        """Orchestre le placement d'un trade."""
        self.log.info(f"--- DÉBUT EXÉCUTION TRADE {symbol} ---")

        trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        # --- Version 1.0.0: Appel direct ---
        price_info = self._mt5.symbol_info_tick(symbol)
        # --- Fin Version ---

        if not price_info: self.log.error(f"Tick introuvable {symbol}. Ordre annulé."); return
        price = price_info.ask if direction == BUY else price_info.bid

        # --- Version 1.0.0: Appel RiskManager ---
        # Assurez-vous que la signature de calculate_trade_parameters est compatible
        volume, sl, tp = risk_manager.calculate_trade_parameters(account_info.equity, price, ohlc_data, trade_signal)
        # --- Fin Version ---

        if volume > 0 and sl > 0 and tp > 0: # Check basique
            self.log.info(f"Paramètres calculés: {direction} {volume:.4f} {symbol} @ ~{price:.5f}, SL={sl:.5f}, TP={tp:.5f}")
            # --- Version 1.0.0: Pas de dry run, pas de vérifs pré-ordre ---
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)
            # --- Fin Version ---
            if result and result.order > 0:
                atr_value = 0 # Calcul ATR peut différer en v1.0.0
                try: atr_value = risk_manager.calculate_atr(ohlc_data, 14) or 0 # Exemple
                except Exception: pass
                self._trade_context[result.order] = { 'symbol': symbol, 'direction': direction, 'open_time': datetime.utcnow().isoformat(), 'pattern_trigger': pattern_name, 'volatility_atr': atr_value }
        else: self.log.warning(f"Trade {symbol} annulé: Vol={volume}, SL={sl}, TP={tp}.")


    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        """Place un ordre marché (version 1.0.0)."""
        comment = f"KasperBot-{pattern_name}"[:31]
        request = { "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume), "type": order_type, "price": float(price), "sl": float(sl), "tp": float(tp), "deviation": 20, "magic": magic_number, "comment": comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC, }
        self.log.debug(f"Envoi requête ordre: {request}")
        try:
            # --- Version 1.0.0: Appel direct ---
            result = self._mt5.order_send(request)
            # --- Fin Version ---
        except Exception as e: self.log.critical(f"Exception order_send: {e}", exc_info=True); return None

        # --- Version 1.0.0: Gestion erreur simple ---
        if result is None: self.log.error(f"Échec critique order_send=None. Erreur MT5: {self._mt5.last_error()}"); return None
        if result.retcode == mt5.TRADE_RETCODE_DONE: self.log.info(f"Ordre placé: Ticket #{result.order}, Retcode: {result.retcode}"); return result
        else: self.log.error(f"Échec ordre: retcode={result.retcode}, commentaire={result.comment}"); return None
        # --- Fin Version ---


    def check_for_closed_trades(self, magic_number: int):
        """Vérifie trades fermés (version 1.0.0)."""
        # --- Version 1.0.0: Logique potentiellement différente ---
        try:
            from_date = datetime.utcnow() - timedelta(days=7) # Exemple
            # --- Version 1.0.0: Appel direct ---
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())
            # --- Fin Version ---
            if history_deals is None: self.log.warning("Historique deals indisponible."); return

            # Logique simplifiée possible en v1.0.0, basée sur les tickets ouverts connus
            closed_tickets_found = set()
            currently_open_tickets = set(self._trade_context.keys())

            deals_by_order_id = {}
            for deal in history_deals:
                 if deal.magic == magic_number and deal.order in currently_open_tickets:
                     deals_by_order_id.setdefault(deal.order, []).append(deal)

            for order_id in currently_open_tickets:
                 order_deals = deals_by_order_id.get(order_id, [])
                 # Si un deal de sortie existe pour cet ordre ID
                 if any(d.entry == mt5.DEAL_ENTRY_OUT for d in order_deals):
                     closed_tickets_found.add(order_id)

            for order_id in closed_tickets_found:
                if order_id in self._trade_context:
                    context = self._trade_context.pop(order_id)
                    self.log.info(f"Trade (Ordre #{order_id}) détecté comme fermé.")
                    position_deals = deals_by_order_id.get(order_id, [])
                    exit_deal = next((d for d in reversed(position_deals) if d.entry == mt5.DEAL_ENTRY_OUT), None)
                    if exit_deal:
                        total_pnl = sum(d.profit for d in position_deals) # Pnl basé sur deals de l'ordre
                        trade_record = { 'ticket': order_id, 'position_id': exit_deal.position_id, # Position ID du deal de sortie
                                         'symbol': context['symbol'], 'direction': context['direction'], 'open_time': context['open_time'],
                                         'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(), 'pnl': total_pnl,
                                         'pattern_trigger': context['pattern_trigger'], 'volatility_atr': context.get('volatility_atr', 0) }
                        self._archive_trade(trade_record)
                        self.professional_journal.record_trade(trade_record, self.get_account_info())
                    else: self.log.warning(f"Deal sortie non trouvé pour ordre fermé {order_id}")
        except Exception as e: self.log.error(f"Erreur check_for_closed_trades: {e}", exc_info=True)
        # --- Fin Version ---


    def _archive_trade(self, trade_record: dict):
        """Archive simple CSV."""
        # ... (inchangé a priori) ...
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
        except IOError as e: self.log.error(f"Erreur archivage trade #{trade_record['ticket']}: {e}")

    def get_account_info(self):
        """Récupère infos compte."""
        # --- Version 1.0.0: Appel direct ---
        try: return self._mt5.account_info()
        except Exception as e: self.log.error(f"Erreur get_account_info: {e}"); return None
        # --- Fin Version ---

    def modify_position(self, ticket, sl, tp):
        """Modifie SL/TP (version 1.0.0)."""
        request = { "action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": float(sl), "tp": float(tp) }
        self.log.debug(f"Requête modif SL/TP ticket {ticket}: SL={sl}, TP={tp}")
        # --- Version 1.0.0: Pas de dry run, appel direct, gestion erreur simple ---
        try:
            result = self._mt5.order_send(request)
            if not result: self.log.error(f"Échec modif pos #{ticket}, order_send=None. Err MT5: {self._mt5.last_error()}")
            elif result.retcode != mt5.TRADE_RETCODE_DONE: self.log.error(f"Échec modif pos #{ticket}: Code={result.retcode}, Cmt={result.comment}")
            else: self.log.info(f"Position #{ticket} modifiée: SL={sl:.5f}, TP={tp:.5f}")
        except Exception as e: self.log.critical(f"Exception modify_position ticket {ticket}: {e}", exc_info=True)
        # --- Fin Version ---
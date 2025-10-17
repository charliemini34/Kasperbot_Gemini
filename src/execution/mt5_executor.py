# Fichier: src/execution/mt5_executor.py
# Version: 15.0.0 (Execution-Hardened)
# Dépendances: MetaTrader5, pandas, logging
# Description: Exécuteur d'ordres MT5 avec gestion du remplissage IOC et suivi des trades robustes.

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
from datetime import datetime, timedelta
from src.constants import BUY, SELL

class MT5Executor:
    def __init__(self, mt5_connection):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'
        self._trade_context = {} # Contexte pour l'archivage

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

    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number):
        """Orchestre le placement d'un trade avec une journalisation détaillée."""
        self.log.info(f"--- DÉBUT DE L'EXÉCUTION DU TRADE POUR {symbol} ---")
        
        trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)
        
        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return

        price = price_info.ask if direction == BUY else price_info.bid
        volume, sl, tp = risk_manager.calculate_trade_parameters(account_info.equity, price, direction, ohlc_data)

        if volume > 0:
            self.log.info(f"Paramètres de l'ordre: {direction} {volume:.2f} lot(s) de {symbol} @ {price:.5f}, SL={sl:.5f}, TP={tp:.5f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)
            
            if result and result.order > 0:
                # Stocker le contexte pour l'archivage futur
                self._trade_context[result.order] = {
                    'symbol': symbol, 'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'volatility_atr': risk_manager.calculate_atr(ohlc_data, 14) or 0
                }
        else:
            self.log.warning(f"Trade sur {symbol} annulé car le volume est de 0.0.")

    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        """Place un ordre de marché avec une gestion robuste des erreurs et un remplissage IOC."""
        comment = f"KasperBot-{pattern_name}"[:31]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC, # --- MODIFICATION CRITIQUE ---
        }
        
        self.log.debug(f"Envoi de la requête d'ordre : {request}")
        try:
            result = self._mt5.order_send(request)
        except Exception as e:
            self.log.critical(f"Exception lors de l'envoi de l'ordre : {e}", exc_info=True)
            return None

        if result is None:
            self.log.error(f"Échec critique de l'envoi. order_send a retourné None. Erreur MT5: {self._mt5.last_error()}")
            return None

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Ordre placé avec succès: Ticket #{result.order}, Retcode: {result.retcode}")
            return result
        else:
            self.log.error(f"Échec de l'envoi de l'ordre: retcode={result.retcode}, commentaire={result.comment}")
            return None

    def check_for_closed_trades(self, magic_number: int):
        """Vérifie et archive les trades fermés en se basant sur l'historique."""
        try:
            from_date = datetime.utcnow() - timedelta(days=7) # Interroger sur une semaine pour être sûr
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())
            
            if history_deals is None:
                self.log.warning("Impossible de récupérer l'historique des transactions pour archivage.")
                return

            closed_tickets = set()
            # Identifier les tickets de sortie avec le bon magic number
            for deal in history_deals:
                if deal.magic == magic_number and deal.entry == 1: # 1 = DEAL_ENTRY_OUT
                    closed_tickets.add(deal.position_id)

            for ticket in closed_tickets:
                if ticket in self._trade_context:
                    context = self._trade_context.pop(ticket)
                    # Trouver le deal de sortie correspondant pour obtenir le PnL
                    exit_deal = next((d for d in history_deals if d.position_id == ticket and d.entry == 1), None)
                    if exit_deal:
                        trade_record = {
                            'ticket': ticket,
                            'symbol': context['symbol'],
                            'direction': context['direction'],
                            'open_time': context['open_time'],
                            'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(),
                            'pnl': exit_deal.profit,
                            'pattern_trigger': context['pattern_trigger'],
                            'volatility_atr': context['volatility_atr']
                        }
                        self._archive_trade(trade_record)
                    else:
                         self.log.warning(f"Contexte trouvé pour le trade fermé #{ticket}, mais le deal de sortie est manquant.")

        except Exception as e:
            self.log.error(f"Erreur lors de la vérification des trades fermés: {e}", exc_info=True)


    def _archive_trade(self, trade_record: dict):
        """Archive un trade dans un fichier CSV."""
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
            self.log.info(f"Trade #{trade_record['ticket']} archivé avec un PnL de {trade_record['pnl']:.2f}$.")
        except IOError as e:
            self.log.error(f"Erreur d'écriture lors de l'archivage du trade #{trade_record['ticket']}: {e}")

    def get_account_info(self):
        try:
            return self._mt5.account_info()
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération des infos du compte: {e}")
            return None
    
    def modify_position(self, ticket, sl, tp):
        """Modifie le SL/TP d'une position ouverte."""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": float(sl),
            "tp": float(tp)
        }
        result = self._mt5.order_send(request)
        
        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_comment = result.comment if result else "Résultat vide"
            self.log.error(f"Échec de la modification de la position #{ticket}: {error_comment}")
        else:
            self.log.info(f"Position #{ticket} modifiée avec succès (SL: {sl}, TP: {tp}).")
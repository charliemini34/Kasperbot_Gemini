import MetaTrader5 as mt5
from datetime import datetime
import logging

class MT5Executor:
    def __init__(self, connection, performance_analyzer):
        self._mt5 = connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.analyzer = performance_analyzer
        self.open_trade_tickets = {pos.ticket for pos in self.get_open_positions()}
        self.newly_closed_trades = 0

    def execute_trade(self, account_info, risk_manager, symbol, direction, score, raw_scores):
        self.log.info(f"Exécution du trade pour {symbol}, Direction: {direction}")
        price_info = self._mt5.symbol_info_tick(symbol)
        if not price_info:
            self.log.error(f"Impossible d'obtenir le prix pour {symbol}. Trade annulé.")
            return None

        trade_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = price_info.ask if direction == "BUY" else price_info.bid
        
        sl, tp = risk_manager.calculate_sl_tp(price, direction)
        volume = risk_manager.calculate_volume(account_info.equity, price, sl)
        
        symbol_info = self._mt5.symbol_info(symbol)
        if volume < symbol_info.volume_min:
            self.log.warning(f"Volume calculé {volume} inférieur au minimum {symbol_info.volume_min}. Trade annulé.")
            return None

        order_request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
            "type": trade_type, "price": price, "sl": sl, "tp": tp, "deviation": 20,
            "magic": 202502, "comment": f"BOTv2/{direction}/{score:.1f}",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = self.create_market_order(order_request)
        if result:
            self.analyzer.log_trade_open(result.order, symbol, direction, datetime.now(), raw_scores)
            self.open_trade_tickets.add(result.order)
        return result

    def check_for_closed_trades(self):
        """Vérifie si des trades précédemment ouverts ont été fermés."""
        currently_open_positions = {pos.ticket for pos in self.get_open_positions()}
        closed_tickets = self.open_trade_tickets - currently_open_positions
        
        for ticket in closed_tickets:
            self.log.info(f"Trade #{ticket} détecté comme fermé.")
            deals = self._mt5.history_deals_get(ticket=ticket)
            if deals:
                exit_deal = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
                if exit_deal:
                    self.analyzer.log_trade_close(ticket, exit_deal.profit, datetime.fromtimestamp(exit_deal.time))
                    self.newly_closed_trades += 1
                else:
                    self.analyzer.log_trade_close(ticket, 0.0, datetime.now()) # Fallback
            
        self.open_trade_tickets = currently_open_positions
        
    def get_newly_closed_trades_count(self):
        count = self.newly_closed_trades
        self.newly_closed_trades = 0
        return count

    def create_market_order(self, order_request):
        self.log.info(f"Envoi de l'ordre: {order_request}")
        result = self._mt5.order_send(order_request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Ordre exécuté avec succès. Ticket: {result.order}")
            return result
        else:
            error = self._mt5.last_error()
            retcode = result.retcode if result else 'N/A'
            self.log.error(f"Échec de l'ordre. Code: {retcode}, Commentaire: {error}")
            return None

    def modify_position(self, ticket, new_sl, new_tp):
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": float(new_sl), "tp": float(new_tp)}
        result = self._mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Position {ticket} modifiée avec succès.")
        else:
            self.log.error(f"Échec de la modification de la position {ticket}. Erreur: {self._mt5.last_error()}")

    def get_open_positions(self, symbol=None):
        positions = self._mt5.positions_get(symbol=symbol)
        return [] if positions is None else positions

    def get_account_info(self):
        return self._mt5.account_info()
            
    def get_daily_pnl(self):
        start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        history = self._mt5.history_deals_get(start_of_day, datetime.now())
        return sum(d.profit for d in history if d.entry == mt5.DEAL_ENTRY_OUT) if history else 0.0
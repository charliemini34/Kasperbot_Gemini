# Fichier: src/execution/mt5_executor.py
# Version améliorée par votre Partenaire de Code

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
from datetime import datetime
from src.constants import BUY, SELL

class MT5Executor:
    """
    Gère l'exécution des ordres et la communication avec l'API MT5.
    v9.4 : Ajoute la validation du Risk/Reward avant de placer l'ordre.
    """
    def __init__(self, mt5_connection):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self._open_trades_context = {}
        self.history_file = 'trade_history.csv'

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        if symbol:
            positions = self._mt5.positions_get(symbol=symbol)
        else:
            positions = self._mt5.positions_get()
        if positions is None: return []
        if magic > 0:
            return [pos for pos in positions if pos.magic == magic]
        return list(positions)

    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number, market_trend, volatility_atr):
        """Orchestre le placement d'un trade, avec validation R/R, et enregistre son contexte."""
        trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)
        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return

        price = price_info.ask if direction == BUY else price_info.bid
        sl, tp = risk_manager.calculate_sl_tp(price, direction, ohlc_data, symbol)

        # NOUVEAU : Validation du ratio Risque/Rendement
        if not risk_manager.check_risk_reward_ratio(price, sl, tp):
            self.log.warning(f"Trade sur {symbol} annulé en raison d'un ratio Risque/Rendement insuffisant.")
            return

        volume = risk_manager.calculate_volume(account_info.equity, price, sl)

        if volume > 0:
            self.log.info(f"Préparation de l'ordre {direction} {volume} lot(s) de {symbol} @ {price:.5f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)
            
            if result:
                self._open_trades_context[result.order] = {
                    'symbol': symbol,
                    'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'market_trend': market_trend,
                    'volatility_atr': volatility_atr
                }
        else:
            self.log.warning("Le volume calculé est de 0.0. L'ordre n'est pas placé.")

    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        comment = f"KB9-{pattern_name}"[:31]
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
            "type": order_type, "price": price, "sl": sl, "tp": tp, "deviation": 20,
            "magic": magic_number, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        # Boucle de tentative simple pour gérer les erreurs non critiques
        for attempt in range(3):
            result = self._mt5.order_send(request)
            if result is None:
                self.log.error(f"Échec critique de l'envoi de l'ordre (tentative {attempt+1}/3). Erreur MT5: {self._mt5.last_error()}")
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"Ordre placé avec succès: Ticket #{result.order}")
                return result
            # Gestion des erreurs courantes qui peuvent être temporaires
            if result.retcode in [mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_OFF]:
                self.log.warning(f"Re-quote ou prix obsolète (tentative {attempt+1}/3). Nouvel essai...")
                time.sleep(1)
                # Mise à jour du prix pour la nouvelle tentative
                price_info = self._mt5.symbol_info_tick(symbol)
                if price_info:
                    request['price'] = price_info.ask if order_type == mt5.ORDER_TYPE_BUY else price_info.bid
            else:
                self.log.error(f"Échec de l'envoi de l'ordre: retcode={result.retcode}, commentaire={result.comment}")
                return None
        
        self.log.error("Échec de l'envoi de l'ordre après 3 tentatives.")
        return None

    def check_for_closed_trades(self, magic_number):
        current_open_tickets = {pos.ticket for pos in self.get_open_positions(magic=magic_number)}
        closed_tickets = set(self._open_trades_context.keys()) - current_open_tickets

        for ticket in closed_tickets:
            self.log.info(f"Trade #{ticket} détecté comme fermé. Archivage...")
            history_deals = self._mt5.history_deals_get(ticket=ticket)
            
            if history_deals:
                exit_deal = next((d for d in history_deals if d.entry == 1), None) # 1 = DEAL_ENTRY_OUT
                if exit_deal:
                    context = self._open_trades_context.pop(ticket)
                    trade_record = {
                        'ticket': ticket, 'symbol': context['symbol'], 'direction': context['direction'],
                        'open_time': context['open_time'], 'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(),
                        'pnl': exit_deal.profit, 'pattern_trigger': context['pattern_trigger'],
                        'market_trend': context['market_trend'], 'volatility_atr': context['volatility_atr']
                    }
                    self._archive_trade(trade_record)
            else:
                # Si aucune information n'est trouvée, on nettoie simplement le contexte pour éviter les erreurs
                self._open_trades_context.pop(ticket, None)

    def _archive_trade(self, trade_record: dict):
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
            self.log.info(f"Trade #{trade_record['ticket']} archivé avec un PnL de {trade_record['pnl']:.2f}$.")
        except Exception as e:
            self.log.error(f"Impossible d'archiver le trade #{trade_record['ticket']}: {e}")

    def get_account_info(self):
        return self._mt5.account_info()
    
    def modify_position(self, ticket, sl, tp):
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
        result = self._mt5.order_send(request)
        if result and result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log.error(f"Échec de la modification de la position #{ticket}: {result.comment}")
        elif result:
            self.log.info(f"Position #{ticket} modifiée avec succès (SL: {sl}, TP: {tp}).")
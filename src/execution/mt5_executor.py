# Fichier: src/execution/mt5_executor.py

import MetaTrader5 as mt5
import logging
import pandas as pd
from datetime import datetime

class MT5Executor:
    """Gère l'exécution des ordres et la communication avec l'API MT5."""
    def __init__(self, mt5_connection, analyzer):
        self._mt5 = mt5_connection
        self._analyzer = analyzer
        self.log = logging.getLogger(self.__class__.__name__)
        self.newly_closed_trades = 0

    def get_open_positions(self, symbol: str, magic: int = 0) -> list:
        """Récupère les positions ouvertes pour un symbole donné, filtrées par code MAGIC."""
        positions = self._mt5.positions_get(symbol=symbol)
        if positions is None:
            return []
        
        if magic > 0:
            return [pos for pos in positions if pos.magic == magic]
        
        return list(positions)

    def execute_trade(self, account_info, risk_manager, symbol, direction, score, raw_scores, ohlc_data):
        """Orchestre le processus complet de placement d'un trade."""
        trade_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)
        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return
            
        price = price_info.ask if direction == "BUY" else price_info.bid
        
        # Le risk_manager a maintenant besoin des données OHLC pour l'ATR
        sl, tp = risk_manager.calculate_sl_tp(price, direction, ohlc_data)
        volume = risk_manager.calculate_volume(account_info.equity, price, sl)
        
        magic_number = risk_manager._config.get('magic_number', 0)

        if volume > 0:
            self.log.info(f"Préparation de l'ordre {direction} {volume} lot(s) de {symbol} @ {price:.3f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number)
            if result:
                # CORRECTION + AMÉLIORATION : Appel de la bonne fonction avec plus de données
                self._analyzer.log_trade_open(
                    ticket=result.order,
                    symbol=symbol,
                    direction=direction,
                    open_time=datetime.now(),
                    final_score=score,
                    raw_scores=raw_scores
                )
        else:
            self.log.warning("Le volume calculé est de 0.0. L'ordre n'est pas placé.")

    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number):
        """Envoie la requête de placement d'ordre à MT5."""
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": magic_number,
            "comment": "Trade by KasperBot v3.5",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = self._mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log.error(f"Échec de l'envoi de l'ordre: retcode={result.retcode}, commentaire={result.comment}")
            return None
        
        self.log.info(f"Ordre placé avec succès: Ticket #{result.order}")
        return result
    
    def get_account_info(self):
        return self._mt5.account_info()

    def modify_position(self, ticket, sl, tp):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }
        result = self._mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log.error(f"Échec de la modification de la position #{ticket}: {result.comment}")
        else:
            self.log.info(f"Position #{ticket} modifiée avec succès.")

    def get_daily_pnl(self):
        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            history = self._mt5.history_deals_get(today, datetime.now())
            if history:
                df = pd.DataFrame(list(history), columns=history[0]._asdict().keys())
                return df[df['entry'] == 1]['profit'].sum() # Somme des profits des "deals de sortie"
            return 0.0
        except Exception as e:
            self.log.error(f"Erreur lors du calcul du PnL journalier : {e}")
            return 0.0
    
    def check_for_closed_trades(self):
        # Cette logique sera gérée par le performance_analyzer
        pass
        
    def get_newly_closed_trades_count(self):
        count = self.newly_closed_trades
        self.newly_closed_trades = 0
        return count
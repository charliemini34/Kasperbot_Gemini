# Fichier: src/execution/mt5_executor.py

import MetaTrader5 as mt5
import logging
from datetime import datetime

class MT5Executor:
    """
    Gère l'exécution des ordres et la communication avec l'API MT5.
    v8.0 : Correction du format du commentaire pour compatibilité maximale.
    """
    def __init__(self, mt5_connection):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.open_trade_tickets = {pos.ticket for pos in self.get_open_positions()}

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        """Récupère les positions ouvertes, filtrées par symbole et/ou code MAGIC."""
        if symbol:
            positions = self._mt5.positions_get(symbol=symbol)
        else:
            positions = self._mt5.positions_get()
            
        if positions is None: return []
        
        if magic > 0:
            return [pos for pos in positions if pos.magic == magic]
        
        return list(positions)

    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name):
        """Orchestre le processus complet de placement d'un trade."""
        trade_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)
        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return
            
        price = price_info.ask if direction == "BUY" else price_info.bid
        
        sl, tp = risk_manager.calculate_sl_tp(price, direction, ohlc_data)
        volume = risk_manager.calculate_volume(account_info.equity, price, sl)
        
        magic_number = risk_manager._config.get('magic_number', 0)

        if volume > 0:
            self.log.info(f"Préparation de l'ordre {direction} {volume} lot(s) de {symbol} @ {price:.3f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)
            if result:
                self.open_trade_tickets.add(result.order)
        else:
            self.log.warning("Le volume calculé est de 0.0. L'ordre n'est pas placé.")

    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        """Envoie la requête de placement d'ordre à MT5 de manière sécurisée."""
        # --- CORRECTION : Commentaire raccourci pour éviter les erreurs d'argument invalide ---
        comment = f"KB8-{pattern_name}"
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
            "type": order_type, "price": price, "sl": sl, "tp": tp, "deviation": 20,
            "magic": magic_number, "comment": comment[:31], # Tronqué à 31 caractères par sécurité
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = self._mt5.order_send(request)
        
        if result is None:
            self.log.error(f"Échec critique de l'envoi de l'ordre. Erreur MT5: {self._mt5.last_error()}")
            return None
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log.error(f"Échec de l'envoi de l'ordre: retcode={result.retcode}, commentaire={result.comment}")
            return None
        
        self.log.info(f"Ordre placé avec succès: Ticket #{result.order}")
        return result

    def get_account_info(self): return self._mt5.account_info()
    
    def modify_position(self, ticket, sl, tp):
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
        result = self._mt5.order_send(request)
        if result and result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log.error(f"Échec modification position #{ticket}: {result.comment}")
        elif result:
            self.log.info(f"Position #{ticket} modifiée avec succès.")
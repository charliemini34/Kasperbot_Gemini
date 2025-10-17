# Fichier: src/execution/mt5_executor.py
# Version: 14.0.0 (Guardian+ Enhanced)
# Dépendances: MetaTrader5, pandas, logging
# Description: Exécuteur d'ordres MT5 avec journalisation enrichie et gestion des erreurs robuste.

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
import time
from datetime import datetime
from src.constants import BUY, SELL

class MT5Executor:
    def __init__(self, mt5_connection):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self._open_trades_context = {}
        self.history_file = 'trade_history.csv'

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        try:
            if symbol:
                positions = self._mt5.positions_get(symbol=symbol)
            else:
                positions = self._mt5.positions_get()
            
            if positions is None:
                self.log.warning(f"Impossible de récupérer les positions: {self._mt5.last_error()}")
                return []
            
            if magic > 0:
                return [pos for pos in positions if pos.magic == magic]
            
            return list(positions)
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
                self._open_trades_context[result.order] = {
                    'symbol': symbol, 'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'market_trend': 'N/A', # Placeholder, à remplir si vous avez cette info
                    'volatility_atr': risk_manager.calculate_atr(ohlc_data, 14) or 0
                }
        else:
            self.log.warning(f"Trade sur {symbol} annulé car le volume est de 0.0.")

    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        """Place un ordre de marché avec une gestion robuste des erreurs."""
        comment = f"KasperBot-{pattern_name}"[:31]
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": order_type, "price": float(price), "sl": float(sl), "tp": float(tp), "deviation": 20,
            "magic": magic_number, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_FOK,
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

    def check_for_closed_trades(self, magic_number):
        """Vérifie et archive les trades qui ont été fermés."""
        current_open_tickets = {pos.ticket for pos in self.get_open_positions(magic=magic_number)}
        # Utilise une copie pour éviter les problèmes de concurrence si le dictionnaire est modifié ailleurs
        context_keys = list(self._open_trades_context.keys())
        closed_tickets = set(context_keys) - current_open_tickets

        for ticket in closed_tickets:
            self.log.info(f"Trade #{ticket} détecté comme fermé. Tentative d'archivage...")
            
            history_deals = self._mt5.history_deals_get(ticket=ticket)
            
            if history_deals and len(history_deals) > 0:
                # Un trade peut avoir plusieurs deals (entrée, modification SL/TP, sortie)
                # On cherche le deal de sortie (entry == 1)
                exit_deal = next((d for d in history_deals if d.entry == 1), None)
                
                if exit_deal:
                    context = self._open_trades_context.pop(ticket, None)
                    if not context: 
                        self.log.warning(f"Le contexte du trade #{ticket} a déjà été retiré. Impossible d'archiver.")
                        continue

                    trade_record = {
                        'ticket': ticket, 'symbol': context['symbol'],
                        'direction': context['direction'], 'open_time': context['open_time'],
                        'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(),
                        'pnl': exit_deal.profit, 'pattern_trigger': context['pattern_trigger'],
                        'market_trend': context['market_trend'], 'volatility_atr': context['volatility_atr']
                    }
                    self._archive_trade(trade_record)
                else:
                    self.log.warning(f"Trade #{ticket} trouvé dans l'historique mais sans deal de sortie. Sera réessayé au prochain cycle.")
            else:
                self.log.warning(f"Trade #{ticket} fermé mais son historique est indisponible pour le moment. Sera réessayé.")

    def _archive_trade(self, trade_record: dict):
        """Archive un trade dans un fichier CSV."""
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
            self.log.info(f"Trade #{trade_record['ticket']} archivé avec un PnL de {trade_record['pnl']:.2f}$.")
        except IOError as e:
            self.log.error(f"Erreur d'écriture lors de l'archivage du trade #{trade_record['ticket']}: {e}")
        except Exception as e:
            self.log.error(f"Erreur inattendue lors de l'archivage du trade #{trade_record['ticket']}: {e}", exc_info=True)

    def get_account_info(self):
        try:
            return self._mt5.account_info()
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération des infos du compte: {e}")
            return None
    
    def modify_position(self, ticket, sl, tp):
        """Modifie le SL/TP d'une position ouverte."""
        request = {
            "action": mt5.TRADE_ACTION_SLTP, "position": ticket,
            "sl": float(sl), "tp": float(tp)
        }
        result = self._mt5.order_send(request)
        
        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log.error(f"Échec de la modification de la position #{ticket}: {result.comment if result else 'Résultat vide'}")
        else:
            self.log.info(f"Position #{ticket} modifiée avec succès (SL: {sl}, TP: {tp}).")
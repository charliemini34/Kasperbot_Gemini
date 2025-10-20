# Fichier: src/execution/mt5_executor.py
# Version: 15.4.0 (Partial-TP-Execution)
# Dépendances: MetaTrader5, pandas, logging, math, src.journal.professional_journal
# Description: Ajoute la fonction close_partial_position et met à jour le contexte.

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
import math # Ajout pour l'arrondi du volume partiel
from datetime import datetime, timedelta
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
        self._trade_context = {} # Stocke les infos des trades ouverts par le bot
        self.professional_journal = ProfessionalJournal(config)
        self.config = config # Garder une référence à la config

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        # ... (inchangé) ...
        try:
            positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
            if positions is None:
                self.log.warning(f"Impossible de récupérer les positions: {self._mt5.last_error()}")
                return []
            # Filtrer par magic number si spécifié
            return [pos for pos in positions if magic == 0 or pos.magic == magic]
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération des positions: {e}", exc_info=True)
            return []

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
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)

            if result and result.order > 0:
                # --- MODIFICATION: Enrichissement du contexte ---
                partial_tp_levels = self.config.get('risk_management', {}).get('partial_tp', {}).get('levels', [])
                num_partial_levels = len(partial_tp_levels)

                # Calcul ATR pour archivage
                ohlc_data_for_atr = self._mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 100)
                atr_value = 0
                if ohlc_data_for_atr is not None:
                     df_atr = pd.DataFrame(ohlc_data_for_atr)
                     if not df_atr.empty:
                        atr_value = risk_manager.calculate_atr(df_atr, 14) or 0

                self._trade_context[result.order] = {
                    'ticket': result.order, # Ajout du ticket pour référence facile
                    'symbol': symbol,
                    'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'initial_volume': volume, # Volume d'ouverture
                    'remaining_volume': volume, # Volume restant (initialement égal)
                    'partial_tp_state': [False] * num_partial_levels, # Track PTP levels hit
                    'sl_initial': sl, # Garder trace du SL initial
                    'volatility_atr': atr_value
                }
                self.log.debug(f"Contexte créé pour trade #{result.order}: {self._trade_context[result.order]}")
                # --- FIN MODIFICATION ---
        else:
            self.log.warning(f"Execute_trade appelé avec volume 0 pour {symbol}.")


    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name):
        # ... (inchangé, mais ajout log erreur volume) ...
        comment = f"KasperBot-{pattern_name}"[:31]
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": order_type, "price": float(price), "sl": float(sl), "tp": float(tp),
            "deviation": 20, "magic": magic_number, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        self.log.debug(f"Envoi requête ordre: {request}")
        try: result = self._mt5.order_send(request)
        except Exception as e:
            self.log.critical(f"Exception envoi ordre : {e}", exc_info=True)
            return None
        if result is None:
            self.log.error(f"Échec critique envoi order_send=None. Erreur MT5: {self._mt5.last_error()}")
            return None
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Ordre placé OK: Ticket #{result.order}, Retcode: {result.retcode}")
            return result
        else:
            self.log.error(f"Échec envoi ordre: retcode={result.retcode}, commentaire={result.comment}")
            if result.retcode == 10014: # Invalid volume
                 symbol_info_debug = self._mt5.symbol_info(symbol)
                 if symbol_info_debug: self.log.error(f"DEBUG VOLUME {symbol}: Reçu 10014. Vol={volume}. Broker: Min={symbol_info_debug.volume_min}, Max={symbol_info_debug.volume_max}, Step={symbol_info_debug.volume_step}, Digits={symbol_info_debug.volume_digits}")
                 else: self.log.error(f"DEBUG VOLUME {symbol}: Reçu 10014. Vol={volume}. Infos symbole indisponibles.")
            return None

    # --- NOUVELLE FONCTION ---
    def close_partial_position(self, position, volume_to_close: float) -> bool:
        """Envoie un ordre pour clôturer partiellement une position."""
        if volume_to_close <= 0:
            self.log.warning(f"Tentative de clôture partielle de #{position.ticket} avec volume nul ou négatif ({volume_to_close}).")
            return False

        symbol_info = self._mt5.symbol_info(position.symbol)
        if not symbol_info:
            self.log.error(f"Impossible d'obtenir les infos pour {position.symbol} lors de clôture partielle.")
            return False
            
        # Arrondir le volume à clôturer au step du broker
        volume_step = symbol_info.volume_step
        if volume_step > 0:
            volume_to_close = math.floor(volume_to_close / volume_step) * volume_step
        else:
             self.log.warning(f"Volume step invalide pour {position.symbol}. Clôture partielle risque échec.")

        # Assurer qu'on ne ferme pas plus que le volume restant et respecte le min
        volume_to_close = round(min(volume_to_close, position.volume), symbol_info.volume_digits) # Arrondir au nb de décimales supporté
        if volume_to_close < symbol_info.volume_min and volume_to_close > 0:
             # Si le volume restant est inférieur au min, on ferme tout ? Ou on ignore ? Ignorons pour l'instant.
             self.log.warning(f"Volume partiel à clôturer ({volume_to_close}) pour #{position.ticket} < min ({symbol_info.volume_min}). Annulation clôture partielle.")
             return False
        if volume_to_close <= 0:
             self.log.warning(f"Volume partiel à clôturer pour #{position.ticket} est 0 après ajustements. Annulation.")
             return False

        # Déterminer le type d'ordre opposé et le prix
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price_info = self._mt5.symbol_info_tick(position.symbol)
        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour clôture partielle de {position.symbol}.")
            return False
        price = price_info.bid if order_type == mt5.ORDER_TYPE_SELL else price_info.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket, # ID de la position à clôturer partiellement
            "symbol": position.symbol,
            "volume": volume_to_close,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": position.magic,
            "comment": f"Partial TP {volume_to_close} lots",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        self.log.info(f"Tentative de clôture partielle de {volume_to_close:.4f} lots pour position #{position.ticket}...")
        self.log.debug(f"Requête clôture partielle: {request}")

        try: result = self._mt5.order_send(request)
        except Exception as e:
            self.log.critical(f"Exception clôture partielle #{position.ticket} : {e}", exc_info=True)
            return False

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Clôture partielle de {volume_to_close:.4f} lots pour #{position.ticket} réussie. Ticket ordre: #{result.order}")
            # Mettre à jour le contexte (sera fait dans risk_manager après confirmation succès)
            return True
        else:
            retcode = result.retcode if result else "None"
            comment = result.comment if result else self._mt5.last_error()
            self.log.error(f"Échec clôture partielle #{position.ticket}: retcode={retcode}, commentaire={comment}")
            return False
    # --- FIN NOUVELLE FONCTION ---

    def check_for_closed_trades(self, magic_number: int):
        # ... (Logique légèrement modifiée pour gérer la suppression du contexte) ...
        try:
            from_date = datetime.utcnow() - timedelta(days=7)
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())
            if history_deals is None: return

            # Trouver tous les tickets de position encore dans notre contexte
            current_context_tickets = list(self._trade_context.keys())
            
            # Récupérer les positions MT5 actuelles pour ce magic number
            mt5_open_positions = self._mt5.positions_get(magic=magic_number)
            mt5_open_tickets = {pos.ticket for pos in mt5_open_positions} if mt5_open_positions else set()

            for ticket in current_context_tickets:
                # Si un ticket est dans notre contexte mais plus dans MT5 -> il est clôturé
                if ticket not in mt5_open_tickets:
                    context = self._trade_context.pop(ticket) # Supprimer du contexte
                    # Trouver le deal de sortie correspondant (peut y en avoir plusieurs si PTP)
                    # On cherche le dernier deal de sortie pour ce position_id
                    exit_deals = [d for d in history_deals if d.position_id == ticket and d.entry == 1]
                    if exit_deals:
                         last_exit_deal = max(exit_deals, key=lambda d: d.time)
                         # Le PnL archivé sera celui du dernier deal, ce qui est une approximation
                         # Une meilleure approche sommerait les PnL de tous les deals de sortie
                         total_pnl = sum(d.profit for d in exit_deals)
                         trade_record = {
                            'ticket': ticket, 'symbol': context['symbol'], 'direction': context['direction'],
                            'open_time': context['open_time'],
                            'close_time': datetime.fromtimestamp(last_exit_deal.time).isoformat(),
                            'pnl': total_pnl, # Utilise le PnL total des sorties
                            'pattern_trigger': context['pattern_trigger'],
                            'volatility_atr': context.get('volatility_atr', 0) # Utilise .get pour robustesse
                         }
                         self._archive_trade(trade_record)
                         # Journaliser seulement la clôture finale
                         self.professional_journal.record_trade(trade_record, self.get_account_info())
                    else:
                         self.log.warning(f"Trade #{ticket} clôturé mais deal de sortie introuvable.")

        except Exception as e:
            self.log.error(f"Erreur vérification trades fermés: {e}", exc_info=True)


    def _archive_trade(self, trade_record: dict):
        # ... (inchangé) ...
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
            self.log.info(f"Trade #{trade_record['ticket']} (clôture finale) archivé avec PnL total {trade_record['pnl']:.2f}$.")
        except IOError as e:
            self.log.error(f"Erreur archivage trade #{trade_record['ticket']}: {e}")

    def get_account_info(self):
        # ... (inchangé) ...
        try: return self._mt5.account_info()
        except Exception as e:
            self.log.error(f"Erreur récupération infos compte: {e}")
            return None

    def modify_position(self, ticket, sl, tp):
        # ... (inchangé) ...
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": float(sl), "tp": float(tp)}
        result = self._mt5.order_send(request)
        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_comment = result.comment if result else "Résultat vide"
            self.log.error(f"Échec modification pos #{ticket}: {error_comment}")
        else:
            self.log.info(f"Position #{ticket} modifiée (SL: {sl}, TP: {tp}).")
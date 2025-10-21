# Fichier: src/execution/mt5_executor.py
# Version: 15.1.1 (RiskManager-Call-Fix-2) # <-- Version mise à jour
# Dépendances: MetaTrader5, pandas, logging, src.journal.professional_journal

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
from datetime import datetime, timedelta
from src.constants import BUY, SELL
from src.journal.professional_journal import ProfessionalJournal

class MT5Executor:
    def __init__(self, mt5_connection, config: dict):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'
        self._trade_context = {}
        self.professional_journal = ProfessionalJournal(config)

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

    # --- SIGNATURE MODIFIÉE ---
    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number, trade_signal: dict):
        """Orchestre le placement d'un trade avec une journalisation détaillée."""
        self.log.info(f"--- DÉBUT DE L'EXÉCUTION DU TRADE POUR {symbol} ---")

        trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)

        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return

        price = price_info.ask if direction == BUY else price_info.bid

        # --- APPEL CORRIGÉ ---
        # Utiliser directement calculate_trade_parameters avec tous les arguments requis
        volume, sl, tp = risk_manager.calculate_trade_parameters(
            account_info.equity,
            price, # Utiliser le prix Ask/Bid actuel pour le calcul
            ohlc_data,
            trade_signal # Passer le dictionnaire complet du signal
        )
        # --- FIN CORRECTION APPEL ---

        if volume > 0:
            self.log.info(f"Paramètres de l'ordre: {direction} {volume:.2f} lot(s) de {symbol} @ {price:.5f}, SL={sl:.5f}, TP={tp:.5f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)

            if result and result.order > 0:
                atr_value = 0 # Default value
                try:
                    # Recalculer l'ATR basé sur les données ohlc_data fournies si nécessaire pour le contexte
                    atr_value = risk_manager.calculate_atr(ohlc_data, risk_manager._config.get('risk_management', {}).get('atr_settings', {}).get('default', {}).get('period', 14)) or 0
                except Exception as e:
                    self.log.warning(f"Impossible de calculer l'ATR pour le contexte du trade {symbol}: {e}")

                self._trade_context[result.order] = {
                    'symbol': symbol, 'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'volatility_atr': atr_value
                }
        else:
            self.log.warning(f"Trade sur {symbol} annulé car le volume ({volume}) est de 0.0 ou SL/TP invalide ({sl}/{tp}).")


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
            "type_filling": mt5.ORDER_FILLING_IOC,
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
            # Log plus détaillé pour certains échecs courants
            if result.retcode == mt5.TRADE_RETCODE_INVALID_VOLUME:
                 self.log.error(f"Volume invalide: {volume}. Min: {self._mt5.symbol_info(symbol).volume_min}, Max: {self._mt5.symbol_info(symbol).volume_max}, Step: {self._mt5.symbol_info(symbol).volume_step}")
            elif result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
                 self.log.error(f"SL/TP Invalide: SL={sl}, TP={tp}. Freeze Level: {self._mt5.symbol_info(symbol).trade_freeze_level}")
            return None

    def check_for_closed_trades(self, magic_number: int):
        """Vérifie et archive les trades fermés en se basant sur l'historique."""
        try:
            from_date = datetime.utcnow() - timedelta(days=7) # Regarder l'historique des 7 derniers jours
            # Utiliser history_deals_get pour obtenir les transactions
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())

            if history_deals is None:
                self.log.warning("Impossible de récupérer l'historique des transactions pour archivage.")
                return

            closed_tickets_order_ids = set() # Utiliser l'order ID lié au deal de sortie
            deals_by_position_id = {}

            for deal in history_deals:
                if deal.magic == magic_number:
                     # Regrouper les deals par ID de position
                     if deal.position_id not in deals_by_position_id:
                          deals_by_position_id[deal.position_id] = []
                     deals_by_position_id[deal.position_id].append(deal)

                     # Un deal de sortie (entry == 1) signifie que la position est fermée
                     if deal.entry == mt5.DEAL_ENTRY_OUT or deal.entry == mt5.DEAL_ENTRY_INOUT: # DEAL_ENTRY_OUT = 1
                         # Chercher le deal d'entrée correspondant pour trouver l'ID d'ordre d'ouverture
                         entry_deal = next((d for d in history_deals if d.position_id == deal.position_id and (d.entry == mt5.DEAL_ENTRY_IN or d.entry == mt5.DEAL_ENTRY_INOUT)), None) # DEAL_ENTRY_IN = 0
                         if entry_deal:
                              closed_tickets_order_ids.add(entry_deal.order) # Ajouter l'ID d'ordre d'ouverture aux tickets fermés

            # Maintenant, traiter les tickets fermés en utilisant les IDs d'ordre d'ouverture stockés dans _trade_context
            for order_id in closed_tickets_order_ids:
                if order_id in self._trade_context:
                    context = self._trade_context.pop(order_id) # Retirer le contexte associé à cet ordre
                    
                    # Trouver la position ID associée à cet ordre d'ouverture
                    entry_deal = next((d for d in history_deals if d.order == order_id and d.magic == magic_number and (d.entry == mt5.DEAL_ENTRY_IN or d.entry == mt5.DEAL_ENTRY_INOUT)), None)
                    if not entry_deal:
                         self.log.warning(f"Deal d'entrée manquant pour l'ordre fermé #{order_id}")
                         continue
                         
                    position_id = entry_deal.position_id

                    # Trouver le deal de sortie final pour cette position_id
                    exit_deal = next((d for d in reversed(history_deals) if d.position_id == position_id and (d.entry == mt5.DEAL_ENTRY_OUT or d.entry == mt5.DEAL_ENTRY_INOUT)), None)

                    if exit_deal:
                        # Calculer le PnL total pour cette position_id (somme des profits/pertes de tous les deals associés)
                        total_pnl = sum(d.profit for d in deals_by_position_id.get(position_id, []) if d.magic == magic_number)
                        
                        trade_record = {
                            'ticket': order_id, # Utiliser l'order ID comme référence unique pour l'archivage/journal
                            'position_id': position_id, # Garder aussi l'ID de position MT5
                            'symbol': context['symbol'],
                            'direction': context['direction'],
                            'open_time': context['open_time'],
                            'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(),
                            'pnl': total_pnl, # Utiliser le PnL total calculé
                            'pattern_trigger': context['pattern_trigger'],
                            'volatility_atr': context.get('volatility_atr', 0) # Utiliser .get avec valeur par défaut
                        }
                        self._archive_trade(trade_record)
                        self.professional_journal.record_trade(trade_record, self.get_account_info())
                    else:
                         self.log.warning(f"Contexte trouvé pour l'ordre fermé #{order_id} (Position #{position_id}), mais le deal de sortie est manquant dans l'historique récent.")

        except Exception as e:
            self.log.error(f"Erreur lors de la vérification des trades fermés: {e}", exc_info=True)


    def _archive_trade(self, trade_record: dict):
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
            self.log.info(f"Trade (Ordre #{trade_record['ticket']}, Pos #{trade_record.get('position_id', 'N/A')}) archivé avec un PnL de {trade_record['pnl']:.2f}$.")
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
            "position": ticket, # Utiliser l'ID de la position (ticket) pour la modification
            "sl": float(sl),
            "tp": float(tp)
        }
        result = self._mt5.order_send(request)

        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_comment = result.comment if result else "Résultat vide de order_send"
            error_code = result.retcode if result else "N/A"
            self.log.error(f"Échec de la modification de la position #{ticket}: Code={error_code}, Commentaire={error_comment}")
            # Log specific error if SL/TP is invalid relative to current price or freeze level
            if result and result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
                current_info = self._mt5.symbol_info_tick(self._mt5.positions_get(ticket=ticket)[0].symbol)
                freeze_level = self._mt5.symbol_info(self._mt5.positions_get(ticket=ticket)[0].symbol).trade_freeze_level * self._mt5.symbol_info(self._mt5.positions_get(ticket=ticket)[0].symbol).point
                self.log.error(f"Modify Position #{ticket}: SL={sl:.5f}, TP={tp:.5f}. Current Ask={current_info.ask:.5f}, Bid={current_info.bid:.5f}. Freeze Distance={freeze_level:.5f}")

        else:
            self.log.info(f"Position #{ticket} modifiée avec succès (SL: {sl:.5f}, TP: {tp:.5f}).")
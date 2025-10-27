# Fichier: src/execution/mt5_executor.py
# Version: 18.0.2 (Fix R3.2 - Log 'No Money')
# Dépendances: MetaTrader5, pandas, logging, math, time, src.journal.professional_journal

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
import math
import time
from datetime import datetime, timedelta
from src.constants import BUY, SELL
from src.journal.professional_journal import ProfessionalJournal

# (R3.2) Constante pour 'No Money'
TRADE_RETCODE_NO_MONEY = 10019

class MT5Executor:
    def __init__(self, mt5_connection, config: dict):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'
        self._trade_context = {} 
        self.professional_journal = ProfessionalJournal(config)
        try:
             self.symbol_info = self._mt5.symbol_info(self._mt5.account_info().currency)
        except:
             self.log.warning("Impossible de récupérer symbol_info pour la devise du compte au démarrage.")
             self.symbol_info = None

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

    def execute_trade(self, account_info, risk_manager, symbol, direction, ohlc_data, pattern_name, magic_number, trade_signal: dict):
        """Orchestre le placement d'un trade avec une journalisation détaillée."""
        self.log.info(f"--- DÉBUT DE L'EXÉCUTION DU TRADE POUR {symbol} ---")

        trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)

        if not price_info:
            self.log.error(f"Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return

        price = price_info.ask if direction == BUY else price_info.bid

        volume, sl, tp = risk_manager.calculate_trade_parameters(
            account_info.equity,
            price,
            ohlc_data,
            trade_signal 
        )

        if volume > 0:
            self.log.info(f"Paramètres de l'ordre: {direction} {volume:.2f} lot(s) de {symbol} @ {price:.5f}, SL={sl:.5f}, TP={tp:.5f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name)

            if result and result.order > 0 and result.deal > 0:
                position_id = 0
                try:
                    deal_info = self._mt5.history_deals_get(ticket=result.deal)
                    if deal_info and len(deal_info) > 0:
                        position_id = deal_info[0].position_id
                    else:
                        self.log.warning(f"Deal #{result.deal} non trouvé immédiatement. Tentative 2...")
                        time.sleep(0.5)
                        deal_info = self._mt5.history_deals_get(ticket=result.deal)
                        if deal_info and len(deal_info) > 0:
                            position_id = deal_info[0].position_id
                        
                    if position_id == 0:
                        self.log.error(f"Impossible de récupérer le PositionID pour le Deal #{result.deal} (Ordre #{result.order}). Contexte TP partiel échoué.")
                        return
                        
                except Exception as e:
                     self.log.error(f"Exception récupération PositionID pour Deal #{result.deal}: {e}. Contexte TP partiel échoué.")
                     return

                atr_value = 0
                try:
                    atr_value = risk_manager.calculate_atr(ohlc_data, risk_manager._config.get('risk_management', {}).get('atr_settings', {}).get('default', {}).get('period', 14)) or 0
                except Exception as e:
                    self.log.warning(f"Impossible de calculer l'ATR pour le contexte du trade {symbol}: {e}")

                self.log.info(f"Ordre #{result.order} (Deal #{result.deal}) a créé/modifié la Position #{position_id}.")
                
                if position_id in self._trade_context:
                     self.log.warning(f"La Position #{position_id} existe déjà dans le contexte. Écrasement (gestion renforcement non implémentée).")

                self._trade_context[position_id] = {
                    'order_id': result.order,
                    'symbol': symbol, 'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'volatility_atr': atr_value,
                    'original_volume': volume,
                    'original_sl': sl,
                    'original_tp': tp,
                    'partial_tp_taken_percent': 0.0
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

        # (R3.1) Pré-check de Marge et Validité
        self.log.debug(f"Vérification de la requête (Pré-check): {request}")
        try:
            check_result = self._mt5.order_check(request)
            
            # (R3.1) order_check() retourne retcode=0 pour SUCCÈS
            if not check_result or check_result.retcode != 0:
                error_code = check_result.retcode if check_result else -1
                error_comment = check_result.comment if check_result else "order_check a retourné None"
                
                self.log.error(f"ÉCHEC PRÉ-CHECK Marge/Volume: Code={error_code}, Commentaire={error_comment}")
                
                # --- FIX R3.2 ---
                # Ne pas afficher les détails de marge s'ils sont 0.0 car l'API MT5
                # ne les remplit pas en cas de "No Money" (10019)
                if check_result and check_result.retcode != TRADE_RETCODE_NO_MONEY:
                # --- FIN FIX R3.2 ---
                    self.log.error(f"Détails Pré-check: Solde requis: {check_result.balance}, Marge requise: {check_result.margin}, Marge libre: {check_result.margin_free}")
                
                return None
            
            self.log.debug(f"Pré-check réussi (Code 0). Marge libre restante estimée: {check_result.margin_free:.2f}")

        except Exception as e:
            self.log.critical(f"Exception lors du Pré-check : {e}", exc_info=True)
            return None
        # (Fin R3.1)

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
            self.log.info(f"Ordre placé avec succès: Ticket #{result.order}, Deal #{result.deal}, Retcode: {result.retcode}")
            return result
        else:
            self.log.error(f"Échec de l'envoi de l'ordre: retcode={result.retcode}, commentaire={result.comment}")
            if result.retcode == mt5.TRADE_RETCODE_INVALID_VOLUME:
                 info = self._mt5.symbol_info(symbol)
                 self.log.error(f"Volume invalide: {volume}. Min: {info.volume_min}, Max: {info.volume_max}, Step: {info.volume_step}")
            elif result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
                 info = self._mt5.symbol_info(symbol)
                 self.log.error(f"SL/TP Invalide: SL={sl}, TP={tp}. Freeze Level: {info.trade_freeze_level}")
            return None

    def close_partial_position(self, position_ticket: int, volume_to_close: float, magic_number: int, comment: str = "Partial TP"):
        """Clôture une partie d'une position existante."""
        try:
            pos = self._mt5.positions_get(ticket=position_ticket)
            if not pos or len(pos) == 0:
                self.log.error(f"TP Partiel: Position {position_ticket} introuvable pour clôture.")
                return None
            
            position = pos[0]
            
            symbol_info = self._mt5.symbol_info(position.symbol)
            if not symbol_info:
                 self.log.error(f"TP Partiel: Impossible de récupérer SymbolInfo for {position.symbol}")
                 return None

            if volume_to_close <= 0: return None
            if volume_to_close > position.volume:
                self.log.warning(f"TP Partiel: Volume {volume_to_close} > Volume position {position.volume}. Ajustement.")
                volume_to_close = position.volume

            volume_step = symbol_info.volume_step
            if volume_step > 0:
                volume_to_close = math.floor(volume_to_close / volume_step) * volume_step
            
            if (position.volume - volume_to_close) < symbol_info.volume_min and (position.volume - volume_to_close) > 0:
                 self.log.warning(f"TP Partiel: Le volume restant ({position.volume - volume_to_close}) serait < min ({symbol_info.volume_min}). Clôture totale de la position #{position_ticket}.")
                 volume_to_close = position.volume
            elif volume_to_close < symbol_info.volume_min and volume_to_close < position.volume:
                 self.log.warning(f"TP Partiel: Volume à clôturer {volume_to_close} < Min {symbol_info.volume_min}. Clôture partielle impossible.")
                 return None

            if volume_to_close <= 0:
                 self.log.warning(f"TP Partiel: Volume à clôturer calculé à 0.0 après ajustement. Annulation.")
                 return None

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": position_ticket,
                "symbol": position.symbol,
                "volume": float(volume_to_close),
                "type": mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "deviation": 20,
                "magic": magic_number,
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            # (R3.1) Pré-check de la clôture partielle
            check_result = self._mt5.order_check(request)
            
            if not check_result or check_result.retcode != 0:
                 error_code = check_result.retcode if check_result else -1
                 error_comment = check_result.comment if check_result else 'N/A'
                 self.log.error(f"TP Partiel: Échec Pré-check pour clôture {position_ticket}: {error_comment} (Code: {error_code})")
                 
                 # (R3.2) Log amélioré
                 if check_result and check_result.retcode != TRADE_RETCODE_NO_MONEY:
                     self.log.error(f"Détails Pré-check Clôture: Marge requise: {check_result.margin}, Marge libre: {check_result.margin_free}")
                 return None

            # Envoyer l'ordre de clôture
            result = self._mt5.order_send(request)
            
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"TP PARTIEL: {volume_to_close} lots de la position #{position_ticket} clôturés avec succès (Deal #{result.deal}).")
                return result
            else:
                self.log.error(f"TP Partiel: Échec de clôture #{position_ticket}: {result.comment if result else 'N/A'} (Code: {result.retcode if result else 'N/A'})")
                return None
                
        except Exception as e:
            self.log.error(f"Exception lors de la clôture partielle de #{position_ticket}: {e}", exc_info=True)
            return None

    def update_trade_context_partials(self, position_id: int, percentage_just_closed: float):
        """Met à jour le contexte d'un trade après un TP partiel."""
        if position_id in self._trade_context:
            try:
                current_closed = self._trade_context[position_id].get('partial_tp_taken_percent', 0.0)
                new_total_closed = current_closed + percentage_just_closed
                self._trade_context[position_id]['partial_tp_taken_percent'] = new_total_closed
                self.log.debug(f"Contexte Position #{position_id} mis à jour: {new_total_closed * 100:.1f}% clôturé.")
            except KeyError:
                 self.log.error(f"Contexte #{position_id} corrompu. Impossible de mettre à jour partial_tp_taken_percent.")
        else:
            self.log.warning(f"Impossible de mettre à jour le contexte partiel: Position #{position_id} introuvable dans le contexte.")


    def check_for_closed_trades(self, magic_number: int):
        """Vérifie et archive les trades fermés en se basant sur l'historique."""
        try:
            from_date = datetime.utcnow() - timedelta(days=7) 
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())

            if history_deals is None:
                self.log.warning("Impossible de récupérer l'historique des transactions pour archivage.")
                return

            closed_position_ids = set()
            deals_by_position_id = {}

            for deal in history_deals:
                if deal.magic == magic_number:
                     if deal.position_id not in deals_by_position_id:
                          deals_by_position_id[deal.position_id] = []
                     deals_by_position_id[deal.position_id].append(deal)
                     
                     if deal.entry == mt5.DEAL_ENTRY_OUT or deal.entry == mt5.DEAL_ENTRY_INOUT:
                         closed_position_ids.add(deal.position_id)

            open_positions_tickets = {pos.ticket for pos in self.get_open_positions(magic=magic_number)}
            truly_closed_position_ids = closed_position_ids - open_positions_tickets

            for position_id in truly_closed_position_ids:
                if position_id in self._trade_context:
                    context = self._trade_context.pop(position_id)
                    
                    exit_deal = next((d for d in reversed(deals_by_position_id.get(position_id, [])) if (d.entry == mt5.DEAL_ENTRY_OUT or d.entry == mt5.DEAL_ENTRY_INOUT)), None)

                    if exit_deal:
                        total_pnl = sum(d.profit for d in deals_by_position_id.get(position_id, []) if d.magic == magic_number)
                        
                        trade_record = {
                            'ticket': context.get('order_id', position_id),
                            'position_id': position_id,
                            'symbol': context['symbol'],
                            'direction': context['direction'],
                            'open_time': context['open_time'],
                            'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(),
                            'pnl': total_pnl,
                            'pattern_trigger': context['pattern_trigger'],
                            'volatility_atr': context.get('volatility_atr', 0)
                        }
                        self._archive_trade(trade_record)
                        self.professional_journal.record_trade(trade_record, self.get_account_info())
                    else:
                         self.log.warning(f"Contexte trouvé pour la position fermée #{position_id}, mais le deal de sortie est manquant dans l'historique récent.")
                
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
            "position": ticket,
            "sl": float(sl),
            "tp": float(tp)
        }
        
        # (R3.1) Pré-check de la modification SL/TP
        try:
            check_result = self._mt5.order_check(request)
            
            if not check_result or check_result.retcode != 0:
                 error_code = check_result.retcode if check_result else -1
                 error_comment = check_result.comment if check_result else "order_check a retourné None"
                 self.log.error(f"Échec Pré-check modification #{ticket}: Code={error_code}, Commentaire={error_comment}")
                 
                 # (R3.2) Log amélioré
                 if check_result and check_result.retcode != TRADE_RETCODE_NO_MONEY:
                     self.log.error(f"Détails Pré-check SLTP: Marge requise: {check_result.margin}, Marge libre: {check_result.margin_free}")
                 return
        except Exception as e:
            self.log.error(f"Exception lors du Pré-check de modification SL/TP pour #{ticket}: {e}", exc_info=True)
            return
        
        self.log.debug(f"Pré-check modification SLTP pour #{ticket} réussi. Envoi de l'ordre...")

        # Envoi de l'ordre de modification
        result = self._mt5.order_send(request)

        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_comment = result.comment if result else "Résultat vide de order_send"
            error_code = result.retcode if result else "N/A"
            self.log.error(f"Échec de l'ENVOI de la modification de la position #{ticket}: Code={error_code}, Commentaire={error_comment}")
            if result and result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
                try:
                    pos = self._mt5.positions_get(ticket=ticket)[0]
                    current_info = self._mt5.symbol_info_tick(pos.symbol)
                    symbol_info = self._mt5.symbol_info(pos.symbol)
                    freeze_level = symbol_info.trade_freeze_level * symbol_info.point
                    self.log.error(f"Modify Position #{ticket}: SL={sl:.5f}, TP={tp:.5f}. Current Ask={current_info.ask:.5f}, Bid={current_info.bid:.5f}. Freeze Distance={freeze_level:.5f}")
                except Exception: pass
        else:
            self.log.info(f"Position #{ticket} modifiée avec succès (SL: {sl:.5f}, TP: {tp:.5f}).")
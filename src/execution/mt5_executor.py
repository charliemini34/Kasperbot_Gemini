
# Fichier: src/execution/mt5_executor.py
# Version: 1.1.1 (FIX-1)
# Dépendances: MetaTrader5, pandas, logging, src.journal.professional_journal, time

import MetaTrader5 as mt5
import logging
import pandas as pd
import os
import time # Ajouté pour les retries
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

        # --- CORRECTION [FIX-1] ---
        # Définir la liste par défaut en utilisant les entiers (int) pour les codes
        # que la librairie MT5 n'expose pas toujours comme attributs (ex: SERVER_BUSY).
        # Les codes dans config.yaml (ex: 10004) sont déjà des entiers.
        default_retry_codes = [
            mt5.TRADE_RETCODE_REQUOTE,     # 10004 (Généralement disponible)
            mt5.TRADE_RETCODE_PRICE_CHANGED, # 10006 (Généralement disponible)
            10018, # TRADE_RETCODE_SERVER_BUSY (Utiliser l'entier)
            10021, # TRADE_RETCODE_BROKER_BUSY (Utiliser l'entier)
            10017  # TRADE_RETCODE_CONNECTION (Utiliser l'entier)
        ]
        # --- FIN CORRECTION [FIX-1] ---

        # Récupérer les codes d'erreur retryables depuis la config ou utiliser la liste par défaut corrigée
        self._retryable_codes = config.get('execution', {}).get('retryable_retcodes', default_retry_codes)
        self._max_retries = config.get('execution', {}).get('max_retries', 3)
        self._retry_delay = config.get('execution', {}).get('retry_delay_seconds', 1.0)


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
        trade_id = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{symbol}-{pattern_name}" # ID Unique pour le log
        self.log.info(f"[{trade_id}] --- DÉBUT DE L'EXÉCUTION DU TRADE POUR {symbol} ---")

        trade_type = mt5.ORDER_TYPE_BUY if direction == BUY else mt5.ORDER_TYPE_SELL
        price_info = self._mt5.symbol_info_tick(symbol)

        if not price_info:
            self.log.error(f"[{trade_id}] Impossible d'obtenir le tick pour {symbol}. Ordre annulé.")
            return

        price = price_info.ask if direction == BUY else price_info.bid

        volume, sl, tp = risk_manager.calculate_trade_parameters(
            account_info.equity, price, ohlc_data, trade_signal
        )

        if volume > 0:
            # --- [MM-1] Contrôle de Marge Pré-Trade (Approximation) ---
            symbol_info = self._mt5.symbol_info(symbol)
            if not symbol_info:
                 self.log.error(f"[{trade_id}] Impossible d'obtenir symbol_info pour {symbol}. Impossible de vérifier la marge.")
                 return

            contract_size = symbol_info.trade_contract_size
            leverage = account_info.leverage
            
            # Formule de marge plus précise
            required_margin = 0
            try:
                margin_calc = self._mt5.order_calc_margin(
                    mt5.TRADE_ACTION_DEAL, symbol, volume, price
                )
                if margin_calc is not None:
                     required_margin = margin_calc
                else:
                     # Fallback si order_calc_margin échoue (ex: symbole non-margin)
                     self.log.warning(f"[{trade_id}] order_calc_margin a échoué. Utilisation approximation levier.")
                     required_margin = (volume * contract_size * price) / leverage if leverage > 0 else (volume * contract_size * price)
            except Exception as e:
                 self.log.error(f"[{trade_id}] Exception durant order_calc_margin: {e}. Utilisation approximation levier.")
                 required_margin = (volume * contract_size * price) / leverage if leverage > 0 else (volume * contract_size * price)


            if account_info.margin_free < required_margin:
                 self.log.error(f"[{trade_id}] Marge insuffisante pour {symbol}. Requis (calculé): {required_margin:.2f} {account_info.currency}, Dispo: {account_info.margin_free:.2f} {account_info.currency}. Ordre annulé.")
                 return
            self.log.info(f"[{trade_id}] Vérification marge OK. Requis (calculé): {required_margin:.2f}, Dispo: {account_info.margin_free:.2f}")
            # --- Fin [MM-1] ---

            self.log.info(f"[{trade_id}] Paramètres de l'ordre: {direction} {volume:.2f} lot(s) de {symbol} @ {price:.5f}, SL={sl:.5f}, TP={tp:.5f}")
            result = self.place_order(symbol, trade_type, volume, price, sl, tp, magic_number, pattern_name, trade_id) # Passe trade_id

            if result and result.order > 0:
                atr_value = 0
                try:
                    atr_value = risk_manager.calculate_atr(ohlc_data, risk_manager._config.get('risk_management', {}).get('atr_settings', {}).get('default', {}).get('period', 14)) or 0
                except Exception as e:
                    self.log.warning(f"[{trade_id}] Impossible de calculer l'ATR pour le contexte du trade {symbol}: {e}")

                self._trade_context[result.order] = {
                    'trade_id': trade_id, # Stocker l'ID unique
                    'symbol': symbol, 'direction': direction,
                    'open_time': datetime.utcnow().isoformat(),
                    'pattern_trigger': pattern_name,
                    'volatility_atr': atr_value,
                    'volume_initial': volume, # Garder trace du volume initial pour TP partiels
                    'position_id': None # Sera mis à jour lorsque le deal d'entrée est vu (ou par 'result.position_id' si disponible)
                }
                # Tenter d'assigner position_id si le 'result' de order_send le contient
                if hasattr(result, 'position_id'):
                     self._trade_context[result.order]['position_id'] = result.position_id

        else:
            self.log.warning(f"[{trade_id}] Trade sur {symbol} annulé car le volume ({volume}) est de 0.0 ou SL/TP invalide ({sl}/{tp}).")

    # --- [MT5-1] Robustesse place_order ---
    def place_order(self, symbol, order_type, volume, price, sl, tp, magic_number, pattern_name, trade_id):
        """Place un ordre de marché avec retries et gestion robuste des erreurs."""
        comment = f"KasperBot-{pattern_name}"[:31]
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": order_type, "price": float(price), "sl": float(sl), "tp": float(tp),
            "deviation": 20, "magic": magic_number, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for attempt in range(self._max_retries):
            self.log.debug(f"[{trade_id}] Envoi de la requête d'ordre (Tentative {attempt + 1}/{self._max_retries}): {request}")
            result = None
            error_details = ""
            try:
                result = self._mt5.order_send(request)
            except Exception as e:
                self.log.critical(f"[{trade_id}] Exception lors de l'envoi de l'ordre (Tentative {attempt + 1}): {e}", exc_info=True)
                error_details = f"Exception: {e}"
                result = None # Assurer que result est None en cas d'exception

            if result is None:
                mt5_error = self._mt5.last_error()
                self.log.error(f"[{trade_id}] Échec critique (Tentative {attempt + 1}). order_send a retourné None. Erreur MT5: {mt5_error}. Détails: {error_details}")
                # Pas de retry si l'appel API MT5 lui-même échoue critiquement
                return None

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"[{trade_id}] Ordre placé avec succès: Ticket #{result.order}, PositionID #{getattr(result, 'position_id', 'N/A')}, Retcode: {result.retcode} (Tentative {attempt + 1})")
                return result
            else:
                self.log.error(f"[{trade_id}] Échec de l'envoi (Tentative {attempt + 1}): retcode={result.retcode}, commentaire={result.comment}")

                # Utiliser la liste _retryable_codes (qui contient maintenant des entiers)
                if result.retcode in self._retryable_codes and attempt < self._max_retries - 1:
                     delay = self._retry_delay * (2 ** attempt) # Backoff exponentiel
                     self.log.warning(f"[{trade_id}] Erreur retryable ({result.retcode}). Nouvelle tentative dans {delay:.1f}s...")
                     time.sleep(delay)
                else:
                    # Log plus détaillé pour certains échecs non retryables
                    if result.retcode == mt5.TRADE_RETCODE_INVALID_VOLUME:
                         symbol_info = self._mt5.symbol_info(symbol)
                         if symbol_info:
                              self.log.error(f"[{trade_id}] Volume invalide: {volume}. Min: {symbol_info.volume_min}, Max: {symbol_info.volume_max}, Step: {symbol_info.volume_step}")
                         else:
                              self.log.error(f"[{trade_id}] Volume invalide: {volume}. Impossible de récupérer symbol_info.")
                    elif result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
                         symbol_info = self._mt5.symbol_info(symbol)
                         if symbol_info:
                              self.log.error(f"[{trade_id}] SL/TP Invalide: SL={sl}, TP={tp}. Freeze Level: {symbol_info.trade_freeze_level}")
                         else:
                              self.log.error(f"[{trade_id}] SL/TP Invalide: SL={sl}, TP={tp}. Impossible de récupérer symbol_info.")
                    elif result.retcode == mt5.TRADE_RETCODE_NO_MONEY:
                         acc_info = self.get_account_info()
                         if acc_info:
                              self.log.error(f"[{trade_id}] Pas assez d'argent. Marge libre: {acc_info.margin_free:.2f} {acc_info.currency}")
                         else:
                              self.log.error(f"[{trade_id}] Pas assez d'argent. Impossible de récupérer account_info.")

                    self.log.error(f"[{trade_id}] Échec final de l'ordre après {attempt + 1} tentative(s).")
                    return None # Échec final après retries ou pour erreur non retryable

        return None # Au cas où la boucle se termine sans retourner

    # --- Fin [MT5-1] ---

    def check_for_closed_trades(self, magic_number: int):
        """Vérifie et archive les trades fermés en se basant sur l'historique."""
        try:
            from_date = datetime.utcnow() - timedelta(days=7) # Regarder l'historique des 7 derniers jours
            history_deals = self._mt5.history_deals_get(from_date, datetime.utcnow())

            if history_deals is None:
                self.log.warning("Impossible de récupérer l'historique des transactions pour archivage.")
                return

            closed_tickets_order_ids = set()
            deals_by_position_id = {}

            # Mettre à jour les position_id dans le contexte si manquants
            # Et construire la map des deals
            for deal in history_deals:
                if deal.magic == magic_number:
                     # Regrouper les deals par ID de position
                     if deal.position_id not in deals_by_position_id:
                          deals_by_position_id[deal.position_id] = []
                     deals_by_position_id[deal.position_id].append(deal)

                     # Si c'est un deal d'entrée, associer l'order_id au position_id dans le contexte
                     if (deal.entry == mt5.DEAL_ENTRY_IN or deal.entry == mt5.DEAL_ENTRY_INOUT) and deal.order in self._trade_context:
                          if self._trade_context[deal.order].get('position_id') is None:
                               self._trade_context[deal.order]['position_id'] = deal.position_id
                               self.log.debug(f"[{self._trade_context[deal.order].get('trade_id')}] Contexte mis à jour: Ordre #{deal.order} -> Position #{deal.position_id}")


                     # Un deal de sortie signifie que la position est fermée
                     if deal.entry == mt5.DEAL_ENTRY_OUT or deal.entry == mt5.DEAL_ENTRY_INOUT:
                         # Chercher le deal d'entrée correspondant pour trouver l'ID d'ordre d'ouverture
                         entry_deal = next((d for d in history_deals if d.position_id == deal.position_id and (d.entry == mt5.DEAL_ENTRY_IN or d.entry == mt5.DEAL_ENTRY_INOUT) and d.magic == magic_number), None)
                         if entry_deal:
                              closed_tickets_order_ids.add(entry_deal.order) # Ajouter l'ID d'ordre d'ouverture aux tickets fermés

            # Maintenant, traiter les tickets fermés en utilisant les IDs d'ordre d'ouverture stockés dans _trade_context
            for order_id in list(self._trade_context.keys()): # Itérer sur une copie pour pouvoir supprimer
                if order_id in closed_tickets_order_ids:
                    context = self._trade_context.pop(order_id)
                    trade_id = context.get('trade_id', f"unknown-{order_id}") # Récupérer l'ID unique

                    # Trouver la position ID associée à cet ordre d'ouverture (soit depuis contexte, soit re-chercher)
                    position_id = context.get('position_id')
                    if position_id is None:
                         entry_deal = next((d for d in history_deals if d.order == order_id and d.magic == magic_number and (d.entry == mt5.DEAL_ENTRY_IN or d.entry == mt5.DEAL_ENTRY_INOUT)), None)
                         if not entry_deal:
                             self.log.warning(f"[{trade_id}] Deal d'entrée manquant pour l'ordre fermé #{order_id}")
                             continue
                         position_id = entry_deal.position_id
                    
                    if position_id not in deals_by_position_id:
                         self.log.warning(f"[{trade_id}] Contexte trouvé pour Ordre #{order_id} (Pos #{position_id}), mais aucun deal trouvé pour cette position_id.")
                         continue

                    # Trouver le deal de sortie final pour cette position_id
                    exit_deal = next((d for d in reversed(deals_by_position_id[position_id]) if (d.entry == mt5.DEAL_ENTRY_OUT or d.entry == mt5.DEAL_ENTRY_INOUT) and d.magic == magic_number), None)

                    if exit_deal:
                        # Calculer le PnL total pour cette position_id (somme des profits/pertes de tous les deals associés)
                        total_pnl = sum(d.profit for d in deals_by_position_id[position_id] if d.magic == magic_number)
                        
                        trade_record = {
                            'trade_id': trade_id, # Utiliser notre ID unique
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
                        self.log.info(f"[{trade_id}] Trade fermé et archivé. Ordre #{order_id}, Pos #{position_id}, PnL: {total_pnl:.2f}")
                    else:
                         self.log.warning(f"[{trade_id}] Contexte trouvé pour Ordre #{order_id} (Pos #{position_id}), mais le deal de sortie est manquant dans l'historique récent.")

        except Exception as e:
            self.log.error(f"Erreur lors de la vérification des trades fermés: {e}", exc_info=True)


    def _archive_trade(self, trade_record: dict):
        """Archive le trade dans un fichier CSV."""
        trade_id = trade_record.get('trade_id', 'N/A')
        try:
            df = pd.DataFrame([trade_record])
            file_exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not file_exists, index=False)
        except IOError as e:
            self.log.error(f"[{trade_id}] Erreur d'écriture lors de l'archivage du trade #{trade_record['ticket']}: {e}")

    def get_account_info(self):
        try:
            return self._mt5.account_info()
        except Exception as e:
            self.log.error(f"Erreur lors de la récupération des infos du compte: {e}")
            return None

    def modify_position(self, ticket, sl, tp, trade_id="MODIFY"):
        """Modifie le SL/TP d'une position ouverte."""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket, # Utiliser l'ID de la position (ticket) pour la modification
            "sl": float(sl),
            "tp": float(tp)
        }
        self.log.info(f"[{trade_id}-{ticket}] Tentative de modification SL={sl:.5f}, TP={tp:.5f}")
        result = self._mt5.order_send(request)

        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_comment = result.comment if result else "Résultat vide de order_send"
            error_code = result.retcode if result else "N/A"
            self.log.error(f"[{trade_id}-{ticket}] Échec modification: Code={error_code}, Commentaire={error_comment}")
            if result and result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
                try:
                    pos_info = self._mt5.positions_get(ticket=ticket)
                    if pos_info and len(pos_info) > 0:
                        symbol = pos_info[0].symbol
                        current_info = self._mt5.symbol_info_tick(symbol)
                        symbol_info = self._mt5.symbol_info(symbol)
                        if current_info and symbol_info:
                             freeze_level_points = symbol_info.trade_freeze_level
                             point = symbol_info.point
                             freeze_dist = freeze_level_points * point
                             self.log.error(f"[{trade_id}-{ticket}] Détails INVALID_STOPS: SL={sl:.5f}, TP={tp:.5f}. Ask={current_info.ask:.5f}, Bid={current_info.bid:.5f}. FreezeDist={freeze_dist:.5f} ({freeze_level_points} points)")
                    else:
                         self.log.error(f"[{trade_id}-{ticket}] Impossible de récupérer les infos de la position pour détailler l'erreur INVALID_STOPS.")
                except Exception as detail_err:
                     self.log.error(f"[{trade_id}-{ticket}] Erreur additionnelle lors de la récupération des détails INVALID_STOPS: {detail_err}")
        else:
            self.log.info(f"[{trade_id}-{ticket}] Position modifiée avec succès (SL: {sl:.5f}, TP: {tp:.5f}).")

    # --- Ébauche [Risk-2] TP Partiels ---
    def close_partial_position(self, position_ticket, volume_to_close, trade_id="PARTIAL"):
        """Ferme une partie d'une position ouverte."""
        positions = self._mt5.positions_get(ticket=position_ticket)
        if not positions:
            self.log.error(f"[{trade_id}-{position_ticket}] Position introuvable pour clôture partielle.")
            return None
        
        position = positions[0]
        symbol = position.symbol
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price_info = self._mt5.symbol_info_tick(symbol)
        if not price_info:
            self.log.error(f"[{trade_id}-{position_ticket}] Impossible d'obtenir le tick pour clôture partielle de {symbol}.")
            return None
            
        price = price_info.bid if order_type == mt5.ORDER_TYPE_SELL else price_info.ask
        
        # S'assurer que le volume à fermer ne dépasse pas le volume de la position
        volume_to_close = min(float(volume_to_close), position.volume)
        if volume_to_close <= 0:
             self.log.warning(f"[{trade_id}-{position_ticket}] Volume à fermer est invalide ou nul ({volume_to_close}).")
             return None

        # Vérifier le volume minimum
        symbol_info = self._mt5.symbol_info(symbol)
        if volume_to_close < symbol_info.volume_min:
             self.log.warning(f"[{trade_id}-{position_ticket}] Volume à fermer ({volume_to_close}) < Volume Min ({symbol_info.volume_min}).")
             # Ne pas fermer si le volume est trop petit (MT5 rejetterait)
             return None
        
        # Vérifier le volume step
        volume_step = symbol_info.volume_step
        if volume_step > 0:
             volume_to_close = math.floor(volume_to_close / volume_step) * volume_step
             volume_to_close = round(volume_to_close, 8) # Arrondir pour précision
        
        # Re-vérifier après ajustement au step
        if volume_to_close < symbol_info.volume_min:
             self.log.warning(f"[{trade_id}-{position_ticket}] Volume à fermer après ajustement step ({volume_to_close}) < Volume Min ({symbol_info.volume_min}).")
             return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position_ticket, # Spécifie la position à clôturer partiellement
            "symbol": symbol,
            "volume": volume_to_close,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": position.magic,
            "comment": f"Partial TP {trade_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        self.log.info(f"[{trade_id}-{position_ticket}] Tentative de clôture partielle: {volume_to_close:.2f} lots de {symbol} @ {price:.5f}")
        
        # Utiliser la même logique de retry que place_order
        # NOTE : Pour simplifier, la logique de retry n'est pas dupliquée ici,
        # mais elle devrait idéalement être factorisée dans une méthode commune `_send_robust_order`.
        try:
            result = self._mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"[{trade_id}-{position_ticket}] Clôture partielle réussie. Ordre #{result.order}, Deal #{result.deal}")
                return result
            else:
                error_comment = result.comment if result else "Résultat vide"
                error_code = result.retcode if result else "N/A"
                self.log.error(f"[{trade_id}-{position_ticket}] Échec clôture partielle: Code={error_code}, Commentaire={error_comment}")
                return None
        except Exception as e:
            self.log.critical(f"[{trade_id}-{position_ticket}] Exception lors de la clôture partielle : {e}", exc_info=True)
            return None
    # --- Fin Ébauche [Risk-2] ---
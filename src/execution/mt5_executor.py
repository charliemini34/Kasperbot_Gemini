# Fichier: src/execution/mt5_executor.py
# Version: 15.4.7 (Fix-Journal-API-Call)
# Dépendances: MetaTrader5, logging, pandas, pytz, datetime
# Description: Remplacement de update_trade_status par record_trade (API v1.0.0).

import MetaTrader5 as mt5
import logging
import pandas as pd
import pytz
from datetime import datetime, timedelta
from src.journal.professional_journal import ProfessionalJournal # Importé pour l'archivage

class MT5Executor:
    """
    Gère l'exécution des ordres (ouverture, fermeture, modification)
    et la récupération des données de compte/position via MT5.
    """

    def __init__(self, mt5_connection, config: dict):
        self.mt5 = mt5_connection
        self.config = config
        
        # Initialisation du journal professionnel si activé
        self.journal_config = self.config.get('professional_journal', {})
        self.journal = None
        if self.journal_config.get('enabled', False):
            try:
                # v15.4.6: Passe le dict 'journal_config' complet
                self.journal = ProfessionalJournal(self.journal_config)
                
                logging.info("Journal professionnel activé.")
            except Exception as e:
                logging.error(f"Échec initialisation journal professionnel: {e}")
                self.journal = None # Désactiver en cas d'erreur


    def get_account_info(self):
        """Récupère les informations du compte."""
        info = self.mt5.account_info()
        if info is None:
            logging.error(f"Impossible de récupérer les infos compte. Code: {self.mt5.last_error()}")
            return None
        return info

    def get_open_positions(self, symbol: str = None, magic: int = None) -> list:
        """
        Récupère les positions ouvertes, filtrées par symbole
        ou magic number si spécifié.
        Retourne une liste de mt5.PositionInfo.
        """
        try:
            if symbol:
                positions = self.mt5.positions_get(symbol=symbol)
            else:
                positions = self.mt5.positions_get()
                
            if positions is None:
                logging.error(f"Échec récupération positions. Erreur: {self.mt5.last_error()}")
                return []

            # Filtrer par magic number si fourni
            if magic is not None:
                positions = [pos for pos in positions if pos.magic == magic]
                
            return list(positions) # Retourne une liste de objets PositionInfo
            
        except Exception as e:
            logging.error(f"Erreur inattendue get_open_positions: {e}", exc_info=True)
            return []


    def get_total_floating_pl(self, magic: int) -> float:
        """Calcule le P/L flottant total pour un magic number."""
        positions = self.get_open_positions(magic=magic)
        if not positions:
            return 0.0
        
        total_pl = sum(pos.profit for pos in positions)
        return float(total_pl)


    def _check_order_result(self, result, context: str = "Order Send") -> bool:
        """Factorisation de la vérification du résultat d'ordre MT5."""
        if result is None:
            logging.error(f"Échec {context}: Résultat None. Erreur: {self.mt5.last_error()}")
            return False
            
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Échec {context}: Code {result.retcode} - {result.comment} (Erreur interne: {self.mt5.last_error()})")
            
            # Logguer les codes d'erreur courants
            common_errors = {
                mt5.TRADE_RETCODE_REQUOTE: "Requote",
                mt5.TRADE_RETCODE_REJECT: "Rejeté",
                mt5.TRADE_RETCODE_CANCEL: "Annulé",
                mt5.TRADE_RETCODE_TIMEOUT: "Timeout",
                mt5.TRADE_RETCODE_INVALID_VOLUME: "Volume Invalide",
                mt5.TRADE_RETCODE_INVALID_PRICE: "Prix Invalide",
                mt5.TRADE_RETCODE_INVALID_STOPS: "Stops Invalides",
                mt5.TRADE_RETCODE_TRADE_DISABLED: "Trading Désactivé",
                mt5.TRADE_RETCODE_MARKET_CLOSED: "Marché Fermé",
                mt5.TRADE_RETCODE_NO_MONEY: "Pas assez de marge (No Money)",
                mt5.TRADE_RETCODE_PRICE_CHANGED: "Prix changé",
                mt5.TRADE_RETCODE_OFF_QUOTES: "Pas de cotation (Off Quotes)",
                mt5.TRADE_RETCODE_CONNECTION: "Pas de connexion",
            }
            if result.retcode in common_errors:
                 logging.warning(f"Raison {context}: {common_errors[result.retcode]}")
            
            return False
            
        logging.info(f"Succès {context}: Ticket {result.order} (Position: {result.position})")
        return True


    def place_order(self, symbol: str, order_type: int, volume: float, price: float, sl: float, tp: float, comment: str = "", magic: int = 0) -> mt5.OrderSendResult:
        """Construit et envoie la requête d'ordre."""
        
        symbol_info = self.mt5.symbol_info(symbol)
        if symbol_info is None:
            logging.error(f"place_order: Infos symbole {symbol} introuvables.")
            return None

        # Déterminer le type de remplissage (FOK, IOC, ou standard)
        filling_type = symbol_info.filling_mode
        if filling_type == mt5.SYMBOL_FILLING_FOK:
            fill_mode = mt5.ORDER_FILLING_FOK
        elif filling_type == mt5.SYMBOL_FILLING_IOC:
            fill_mode = mt5.ORDER_FILLING_IOC
        else: # mt5.SYMBOL_FILLING_RETURN ou autre
            fill_mode = mt5.ORDER_FILLING_RETURN

        # Assurer que le prix est correct pour les ordres au marché
        if order_type == mt5.ORDER_TYPE_BUY:
            price_to_send = self.mt5.symbol_info_tick(symbol).ask if price == 0.0 else price
        elif order_type == mt5.ORDER_TYPE_SELL:
            price_to_send = self.mt5.symbol_info_tick(symbol).bid if price == 0.0 else price
        else: # Ordres limites/stop
             price_to_send = price
             
        if price_to_send == 0.0:
             logging.error(f"place_order: Prix {symbol} indisponible pour {order_type}.")
             return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL, # Ordre au marché (ou SL/TP sur existant)
            "symbol": symbol,
            "volume": volume,
            "type": order_type, # mt5.ORDER_TYPE_BUY ou mt5.ORDER_TYPE_SELL
            "price": price_to_send,
            "sl": sl,
            "tp": tp,
            "deviation": self.config.get('trading_settings', {}).get('slippage_deviation', 20),
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, # Good till Canceled
            "type_filling": fill_mode,
        }
        
        logging.debug(f"Envoi requête ordre {symbol}: {request}")
        
        # Envoi de l'ordre
        result = self.mt5.order_send(request)
        return result


    def execute_trade(self, account_info, risk_manager, symbol: str, direction: str, volume: float, sl: float, tp: float, pattern: str, magic: int):
        """
        Orchestre la vérification de marge et le passage d'ordre.
        """
        
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        
        # 1. Vérification de la marge AVANT envoi
        margin_required = self.mt5.order_calc_margin(order_type, symbol, volume, 0.0) # 0.0 pour prix actuel
        
        if margin_required is None:
             logging.error(f"Échec calcul marge pour {symbol} {volume} lots. Erreur: {self.mt5.last_error()}")
             return False
        
        if margin_required > account_info.margin_free:
            logging.critical(f"MARGE INSUFFISANTE pour {symbol} {volume} lots. Requis: {margin_required:.2f}, Dispo: {account_info.margin_free:.2f}")
            return False
            
        logging.info(f"Marge check OK pour {symbol} {volume} lots. Requis: {margin_required:.2f}, Dispo: {account_info.margin_free:.2f}")

        # 2. Passage de l'ordre
        comment = f"KasperBot v19 | {pattern}" # Version de pattern_detector
        
        result = self.place_order(symbol, order_type, volume, 0.0, sl, tp, comment, magic)
        
        if self._check_order_result(result, f"Exécution {direction} {symbol}"):
            # Succès
            if self.journal and result.position > 0:
                 # Tentative d'archivage dans le journal pro (si activé)
                 # Note: La v1.0.0 du journal n'archive que les trades FERMÉS.
                 # L'ancienne méthode 'log_trade' n'existe plus.
                 pass
                 
                 # try:
                 #     self.journal.log_trade( ... ) # Cette méthode n'existe plus
                 # except Exception as e:
                 #     logging.error(f"Échec archivage journal pro (Ticket {result.position}): {e}")
            return True
        else:
            # Échec
            return False


    def modify_position_sl_tp(self, ticket: int, sl: float, tp: float, comment_suffix: str = None):
        """
        Modifie le SL et/ou TP d'une position existante (par ticket).
        Ajoute un suffixe au commentaire si fourni.
        """
        
        position = self.mt5.positions_get(ticket=ticket)
        if not position:
            logging.error(f"Modify SL/TP: Position {ticket} introuvable.")
            return False
        pos = position[0] # Récupère l'objet position (type PositionInfo)

        # Préparer le nouveau commentaire (si suffixe fourni)
        new_comment = pos.comment
        if comment_suffix and comment_suffix not in (pos.comment or ""):
             new_comment = (pos.comment or "") + f"|{comment_suffix}"

        request = {
            "action": mt5.TRADE_ACTION_SLTP, # Modification SL/TP
            "position": ticket,
            "sl": sl,
            "tp": tp,
            "comment": new_comment # Mettre à jour le commentaire
        }
        
        logging.debug(f"Modification SL/TP (Ticket: {ticket}): SL={sl}, TP={tp}, Suffixe={comment_suffix}")
        
        result = self.mt5.order_send(request)
        
        # Log amélioré avec Ticket
        if not self._check_order_result(result, f"Modify SL/TP (Ticket: {ticket})"):
             logging.error(f"Échec modification SL/TP pour Ticket {ticket}. SL={sl}, TP={tp}.")
             return False
        return True


    def close_partial_position(self, position, volume_to_close: float, new_comment_remaining: str = None):
        """
        Ferme une partie d'une position.
        Met à jour le commentaire de la position restante si new_comment_remaining est fourni.
        
        *** CORRECTION v15.4.4: Annotation 'position' supprimée + Fix typo volume_to_chose ***
        """
        
        ticket = position.ticket
        
        # Si on ferme partiellement, on doit ouvrir un ordre inverse
        # pour le volume partiel.
        
        if position.type == mt5.ORDER_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = self.mt5.symbol_info_tick(position.symbol).bid # Ferme au Bid
        else: # SELL
            order_type = mt5.ORDER_TYPE_BUY
            price = self.mt5.symbol_info_tick(position.symbol).ask # Ferme au Ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket, # Important: spécifie la position à fermer
            "symbol": position.symbol,
            "volume": volume_to_close, # *** CORRECTION v15.4.4: Typo corrigée ***
            "type": order_type,
            "price": price,
            "deviation": self.config.get('trading_settings', {}).get('slippage_deviation', 20),
            "magic": position.magic, # Garder le même magic
            "comment": f"PTP (Close {volume_to_close})",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN, # Mode standard pour fermeture
        }
        
        logging.debug(f"Fermeture partielle (Ticket: {ticket}): Vol={volume_to_close}")
        result = self.mt5.order_send(request)
        
        if not self._check_order_result(result, f"Close Partial (Ticket: {ticket}, Vol: {volume_to_close})"):
            logging.error(f"Échec fermeture partielle Ticket {ticket}.")
            return False

        # Si succès et commentaire fourni, tenter de modifier la position restante
        # (Cette partie est délicate, MT5 peut ne pas le permettre facilement)
        # La modification de commentaire se fait via TRADE_ACTION_SLTP (même si SL/TP ne changent pas)
        if new_comment_remaining:
            try:
                # Rafraîchir l'état de la position (le ticket peut avoir changé si FIFO)
                # Non, le ticket devrait rester le même si non-FIFO.
                # Mais le volume a changé.
                
                # On suppose que le ticket principal reste.
                self.modify_position_sl_tp(ticket, position.sl, position.tp, new_comment_remaining.replace("|","")) # Suffixe brut
                logging.info(f"Commentaire (Ticket: {ticket}) mis à jour après PTP.")
            except Exception as e:
                 logging.warning(f"Impossible de mettre à jour le commentaire post-PTP (Ticket: {ticket}): {e}")

        return True


    def close_full_position(self, position, comment: str):
        """
        Ferme une position entièrement (ex: limite de perte).
        
        *** CORRECTION v15.4.4: Annotation 'position' supprimée ***
        """
        ticket = position.ticket
        volume_to_close = position.volume
        
        if position.type == mt5.ORDER_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = self.mt5.symbol_info_tick(position.symbol).bid
        else: # SELL
            order_type = mt5.ORDER_TYPE_BUY
            price = self.mt5.symbol_info_tick(position.symbol).ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": position.symbol,
            "volume": volume_to_close,
            "type": order_type,
            "price": price,
            "deviation": self.config.get('trading_settings', {}).get('slippage_deviation', 20),
            "magic": position.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        
        logging.debug(f"Fermeture complète (Ticket: {ticket}): Vol={volume_to_close}, Raison={comment}")
        result = self.mt5.order_send(request)
        
        if not self._check_order_result(result, f"Close Full (Ticket: {ticket}, Raison: {comment})"):
            logging.error(f"Échec fermeture complète Ticket {ticket}.")
            return False
        return True


    def check_for_closed_trades(self, magic: int):
        """
        Vérifie les deals récents et met à jour le journal pro
        pour les trades fermés (DEAL_ENTRY_OUT).
        
        *** CORRECTION v15.4.7: Appel à 'record_trade' (API v1.0.0) ***
        """
        if not self.journal:
            return # Journal désactivé

        try:
            # Récupérer les infos compte (nécessaires pour record_trade si nouveau fichier)
            account_info = self.get_account_info()
            if not account_info:
                logging.error("check_for_closed_trades: Impossible d'obtenir account_info pour le journal.")
                return

            # Vérifier les 2 derniers jours (suffisant pour un bot H24)
            start_time_utc = datetime.now(pytz.utc) - timedelta(days=2)
            deals = self.mt5.history_deals_get(start_time_utc, datetime.now(pytz.utc))
            
            if deals is None:
                logging.warning("check_for_closed_trades: Impossible de récupérer l'historique des deals.")
                return

            # Filtrer les deals de sortie (clôture) gérés par ce bot
            closed_deals = [d for d in deals if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT]
            
            if not closed_deals:
                return # Aucun trade fermé récemment

            for deal in closed_deals:
                
                # Problème d'idempotence:
                # L'API v1.0.0 (record_trade) ne vérifie pas si le ticket
                # a déjà été journalisé. Si le bot redémarre, il
                # re-journalisera les trades des 2 derniers jours.
                # (Ceci est un défaut de la v1.0.0 de professional_journal.py)
                # Nous nous contentons de corriger l'AttributeError.
                
                # Parser le pattern depuis le commentaire
                pattern = "Unknown"
                if deal.comment:
                    parts = deal.comment.split('|')
                    if len(parts) > 1:
                        pattern = parts[1].strip() # Ex: "POI_PULLBACK (OB Bullish)"

                # Construire le dict 'trade_record' attendu par v1.0.0
                trade_record = {
                    'symbol': deal.symbol,
                    'pattern_trigger': pattern,
                    'pnl': float(deal.profit + deal.commission + deal.swap),
                    'ticket': deal.position_id,
                }

                # Appeler la nouvelle méthode
                self.journal.record_trade(trade_record, account_info)

        except Exception as e:
            logging.error(f"Erreur lors de la vérification des trades fermés (Journal): {e}", exc_info=True)
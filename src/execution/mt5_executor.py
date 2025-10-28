# Fichier: src/execution/mt5_executor.py
# Version: 19.0.1 (Fix R8 - SyntaxError)
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
# (R7) Importer RiskManager pour type hinting (annotation de type)
from src.risk.risk_manager import RiskManager

TRADE_RETCODE_NO_MONEY = 10019

class MT5Executor:
    def __init__(self, mt5_connection, config: dict):
        self._mt5 = mt5_connection
        self.log = logging.getLogger(self.__class__.__name__)
        self.history_file = 'trade_history.csv'
        self._trade_context = {} 
        self.professional_journal = ProfessionalJournal(config)
        self.symbol_info_cache = {}

    def _get_symbol_info(self, symbol: str):
        if symbol not in self.symbol_info_cache:
             info = self._mt5.symbol_info(symbol)
             if not info: self.log.error(f"Infos Symbole indispo: {symbol}"); return None
             self.symbol_info_cache[symbol] = info
        return self.symbol_info_cache[symbol]

    def get_open_positions(self, symbol: str = None, magic: int = 0) -> list:
        try:
            positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
            if positions is None: return []
            return [p for p in positions if magic == 0 or p.magic == magic]
        except Exception as e: self.log.error(f"Erreur get_open_positions: {e}", exc_info=True); return []

    def get_pending_orders(self, symbol: str = None, magic: int = 0) -> list:
        """Récupère ordres limites du bot."""
        try:
            orders = self._mt5.orders_get(symbol=symbol) if symbol else self._mt5.orders_get()
            if orders is None: self.log.warning(f"Récup ordres attente échouée: {self._mt5.last_error()}"); return []
            return [o for o in orders if (magic == 0 or o.magic == magic) and o.type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT]]
        except Exception as e: self.log.error(f"Erreur get_pending_orders: {e}", exc_info=True); return []

    # --- R7 : Fonction principale refondue ---
    def execute_trade(self, account_info, risk_manager: RiskManager, symbol, direction, ohlc_data, pattern_name, magic_number, trade_signal: dict):
        """Orchestre calcul via RM et placement ORDRE LIMITE."""
        self.log.info(f"--- INIT PLACEMENT LIMITE: {symbol} {direction} [{pattern_name}] ---")
        tick = self._mt5.symbol_info_tick(symbol)
        if not tick: self.log.error(f"Tick indispo pour {symbol}. Ordre annulé."); return

        volume, entry_limit, sl, tp = risk_manager.calculate_trade_parameters(
            account_info.equity, tick, ohlc_data, trade_signal
        )

        if volume > 0 and entry_limit > 0:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == BUY else mt5.ORDER_TYPE_SELL_LIMIT
            result = self.place_limit_order(symbol, order_type, volume, entry_limit, sl, tp, magic_number, pattern_name)

            if result and result.order > 0:
                pos_id = result.order # Clé contexte = ID ordre limite (sera ID position sur FTMO)
                atr = 0
                try: atr = risk_manager.calculate_atr(ohlc_data, 14) or 0
                except Exception as e: self.log.warning(f"Erreur calcul ATR contexte {symbol}: {e}")

                self.log.info(f"Ordre Limite #{result.order} (Futur PosID #{pos_id}) -> Contexte R1 créé.")
                if pos_id in self._trade_context: self.log.warning(f"Ctx pour #{pos_id} existe déjà. Écrasement.")

                self._trade_context[pos_id] = {
                    'order_id': result.order, 'symbol': symbol, 'direction': direction,
                    'open_time': None, 'pattern_trigger': pattern_name, 'volatility_atr': atr,
                    'original_volume': volume, 'original_sl': sl, 'original_tp': tp,
                    'partial_tp_taken_percent': 0.0, 'limit_order_price': entry_limit
                }
        else:
            self.log.warning(f"Ordre limite {symbol} annulé: Vol={volume:.2f}, Limite={entry_limit:.5f} (Retracement? SL/TP?)")
    # --- Fin R7 ---

    # --- R7 : Place Ordre Limite ---
    def place_limit_order(self, symbol, order_type, volume, price, sl, tp, magic, pattern):
        """Place un ordre limite."""
        comment = f"KasperBot-L-{pattern}"[:31]
        s_info = self._get_symbol_info(symbol);
        if not s_info: return None
        price = round(price, s_info.digits); sl = round(sl, s_info.digits); tp = round(tp, s_info.digits)

        req = {"action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": float(volume),
               "type": order_type, "price": price, "sl": sl, "tp": tp, "deviation": 0,
               "magic": magic, "comment": comment, "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_RETURN} # FILLING_RETURN souvent nécessaire

        self.log.debug(f"Pré-check LIMITE: {req}")
        try:
            chk = self._mt5.order_check(req)
            if not chk or chk.retcode != 0:
                code=chk.retcode if chk else -1; cmt=chk.comment if chk else "order_check None"
                self.log.error(f"Échec Pré-check LIMITE: Code={code}, Cmt={cmt}")
                return None
            self.log.debug("Pré-check LIMITE OK (Code 0).")
        except Exception as e: self.log.critical(f"Exception Pré-check LIMITE: {e}", exc_info=True); return None

        self.log.debug(f"Envoi ORDRE LIMITE: {req}")
        try: res = self._mt5.order_send(req)
        except Exception as e: self.log.critical(f"Exception Envoi LIMITE: {e}", exc_info=True); return None

        if res is None: self.log.error(f"Échec Envoi LIMITE (None). Erreur MT5: {self._mt5.last_error()}"); return None
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"ORDRE LIMITE placé: Ticket #{res.order}, Retcode: {res.retcode}")
            return res
        else:
            self.log.error(f"Échec Envoi LIMITE: Code={res.retcode}, Cmt={res.comment}")
            return None
    # --- Fin R7 ---

    # --- R7 : Annule Ordre ---
    def cancel_order(self, order_ticket: int):
        """Annule un ordre en attente."""
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": order_ticket}
        self.log.debug(f"Annulation Ordre #{order_ticket}...")
        try:
            res = self._mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"Ordre #{order_ticket} annulé OK.")
                return True
            else:
                cmt=res.comment if res else "N/A"; code=res.retcode if res else "N/A"
                self.log.error(f"Échec Annulation Ordre #{order_ticket}: Code={code}, Cmt={cmt}")
                return False
        except Exception as e: self.log.error(f"Exception Annulation Ordre #{order_ticket}: {e}", exc_info=True); return False
    # --- Fin R7 ---

    # Inchangé: place_order (utilisé pour Clôture Partielle)
    def place_order(self, symbol, order_type, volume, price, sl, tp, magic, pattern):
        # ... (Logique inchangée depuis v18.0.2) ...
        comment = f"KasperBot-M-{pattern}"[:31] # 'M' pour Marché/Modif
        s_info = self._get_symbol_info(symbol);
        if not s_info: return None
        price = round(price, s_info.digits); sl = round(sl, s_info.digits); tp = round(tp, s_info.digits)
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
               "type": order_type, "price": price, "sl": sl, "tp": tp, "deviation": 20,
               "magic": magic, "comment": comment, "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_IOC}
        # Pré-check
        try:
            chk = self._mt5.order_check(req)
            if not chk or chk.retcode != 0:
                code=chk.retcode if chk else -1; cmt=chk.comment if chk else "None"
                self.log.error(f"Échec Pré-check Marché: Code={code}, Cmt={cmt}")
                if chk and chk.retcode != TRADE_RETCODE_NO_MONEY: self.log.error(f"Détails: Marge Req={chk.margin:.2f}, Libre={chk.margin_free:.2f}")
                return None
            self.log.debug(f"Pré-check Marché OK. Marge Libre Estimée: {chk.margin_free:.2f}")
        except Exception as e: self.log.critical(f"Exception Pré-check Marché: {e}"); return None
        # Envoi
        try: res = self._mt5.order_send(req)
        except Exception as e: self.log.critical(f"Exception Envoi Marché: {e}"); return None
        # Résultat
        if res is None: self.log.error(f"Échec Envoi Marché (None). MT5: {self._mt5.last_error()}"); return None
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self.log.info(f"Ordre Marché OK: Ticket #{res.order}, Deal #{res.deal}, Code: {res.retcode}")
            return res
        else:
            self.log.error(f"Échec Envoi Marché: Code={res.retcode}, Cmt={res.comment}")
            # ... Logs détaillés si besoin (INVALID_VOLUME, INVALID_STOPS) ...
            return None

    # Inchangé: close_partial_position
    def close_partial_position(self, pos_ticket: int, vol_close: float, magic: int, comment: str = "Partial TP"):
        # ... (Logique inchangée depuis v18.0.2) ...
        try:
            pos_list = self._mt5.positions_get(ticket=pos_ticket);
            if not pos_list: self.log.error(f"TP Partiel: Pos {pos_ticket} introuvable."); return None
            pos = pos_list[0]; s_info = self._get_symbol_info(pos.symbol)
            if not s_info: return None
            if vol_close <= 0: return None
            if vol_close > pos.volume: vol_close = pos.volume
            step = s_info.volume_step
            if step > 0: vol_close = math.floor(vol_close / step) * step
            vol_min = s_info.volume_min
            if 0 < (pos.volume - vol_close) < vol_min: vol_close = pos.volume # Clôture totale si reste < min
            elif vol_close < vol_min and vol_close < pos.volume: self.log.warning(f"TP Partiel #{pos_ticket}: Vol Close {vol_close:.2f} < Min {vol_min}. Impossible."); return None
            if vol_close <= 0: self.log.warning(f"TP Partiel #{pos_ticket}: Vol Close = 0. Annulé."); return None
            req = {"action": mt5.TRADE_ACTION_DEAL, "position": pos_ticket, "symbol": pos.symbol,
                   "volume": float(vol_close), "type": mt5.ORDER_TYPE_SELL if pos.type == BUY else BUY,
                   "deviation": 20, "magic": magic, "comment": comment[:31],
                   "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
            chk = self._mt5.order_check(req)
            if not chk or chk.retcode != 0:
                 code=chk.retcode if chk else -1; cmt=chk.comment if chk else 'N/A'
                 self.log.error(f"TP Partiel: Échec Pré-check Clôture {pos_ticket}: {cmt} (Code: {code})")
                 if chk and chk.retcode != TRADE_RETCODE_NO_MONEY: self.log.error(f"Détails: Marge Req={chk.margin:.2f}, Libre={chk.margin_free:.2f}")
                 return None
            res = self._mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                self.log.info(f"TP PARTIEL: {vol_close:.2f} lots Pos #{pos_ticket} clôturés OK (Deal #{res.deal}).")
                return res
            else: self.log.error(f"TP Partiel: Échec Clôture #{pos_ticket}: {res.comment if res else 'N/A'} (Code: {res.retcode if res else 'N/A'})"); return None
        except Exception as e: self.log.error(f"Exception Clôture Partielle #{pos_ticket}: {e}", exc_info=True); return None

    # Inchangé: update_trade_context_partials
    def update_trade_context_partials(self, pos_id: int, pct_closed: float):
        # ... (Logique inchangée depuis v19.0.0) ...
        if pos_id in self._trade_context:
            try:
                current = self._trade_context[pos_id].get('partial_tp_taken_percent', 0.0)
                new = current + pct_closed
                self._trade_context[pos_id]['partial_tp_taken_percent'] = new
                self.log.debug(f"Ctx Pos #{pos_id} MàJ: {new * 100:.1f}% clôturé.")
            except KeyError: self.log.error(f"Ctx #{pos_id} corrompu.")
        else: self.log.warning(f"Ctx partiel: Pos #{pos_id} introuvable.")

    # --- R8 : Correction SyntaxError ---
    def check_for_closed_trades(self, magic_number: int):
        """Vérifie et archive trades fermés."""
        try:
            start_utc = datetime.utcnow() - timedelta(days=7)
            deals = self._mt5.history_deals_get(start_utc, datetime.utcnow())
            if deals is None: self.log.warning("Hist deals indispo pour archivage."); return

            closed_pos_ids, deals_by_pos = set(), {}
            for d in deals:
                if d.magic == magic_number:
                     if d.position_id not in deals_by_pos: deals_by_pos[d.position_id] = []
                     deals_by_pos[d.position_id].append(d)
                     if d.entry in [mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT]: closed_pos_ids.add(d.position_id)

            open_tickets = {p.ticket for p in self.get_open_positions(magic=magic_number)}
            truly_closed = closed_pos_ids - open_tickets

            for pos_id in truly_closed:
                if pos_id in self._trade_context:
                    context = self._trade_context.pop(pos_id)
                    exit_deal = next((d for d in reversed(deals_by_pos.get(pos_id, [])) if d.entry in [mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT]), None)

                    if exit_deal:
                        pnl = sum(d.profit for d in deals_by_pos.get(pos_id, []) if d.magic == magic_number)
                        
                        # --- FIX R8 : Remplacer '...' par les vrais champs ---
                        trade_record = {
                            'ticket': context.get('order_id', pos_id), # Utilise order_id si dispo
                            'position_id': pos_id,
                            'symbol': context['symbol'],
                            'direction': context['direction'],
                            'open_time': context['open_time'], # Peut être None si limite non déclenchée et annulée manuellement? À vérifier.
                            'close_time': datetime.fromtimestamp(exit_deal.time).isoformat(),
                            'pnl': pnl,
                            'pattern_trigger': context['pattern_trigger'],
                            'volatility_atr': context.get('volatility_atr', 0)
                        }
                        # --- FIN FIX R8 ---
                        
                        self._archive_trade(trade_record)
                        # Journalisation Pro (si activée)
                        if self.professional_journal.is_enabled():
                           acc_info = self.get_account_info()
                           self.professional_journal.record_trade(trade_record, acc_info)
                    else:
                         self.log.warning(f"Ctx Pos fermée #{pos_id}, mais deal sortie manquant.")
                # Ignorer silencieusement si pos_id fermé n'est pas dans le contexte (ex: trade manuel)

        except Exception as e: self.log.error(f"Erreur check_for_closed_trades: {e}", exc_info=True)
    # --- Fin R8 ---

    # Inchangé: _archive_trade
    def _archive_trade(self, record: dict):
        # ... (Logique inchangée depuis v19.0.0) ...
        try:
            df = pd.DataFrame([record])
            exists = os.path.exists(self.history_file)
            df.to_csv(self.history_file, mode='a', header=not exists, index=False)
            self.log.info(f"Trade archivé (Ordre #{record['ticket']}, Pos #{record.get('position_id', 'N/A')}), PnL: {record['pnl']:.2f}")
        except IOError as e: self.log.error(f"Erreur archivage #{record['ticket']}: {e}")

    # Inchangé: get_account_info
    def get_account_info(self):
        # ... (Logique inchangée depuis v19.0.0) ...
        try: return self._mt5.account_info()
        except Exception as e: self.log.error(f"Erreur get_account_info: {e}"); return None

    # Inchangé: modify_position
    def modify_position(self, ticket, sl, tp):
        # ... (Logique inchangée depuis v19.0.0) ...
        s_info = None
        try: # Essayer de trouver le symbole depuis la position
            pos_list = self._mt5.positions_get(ticket=ticket)
            if pos_list: s_info = self._get_symbol_info(pos_list[0].symbol)
        except Exception: pass
        if not s_info: self.log.warning(f"Infos symbole indispo pour modif SLTP #{ticket}"); digits=5 # Défaut
        else: digits = s_info.digits
        
        sl = round(float(sl), digits); tp = round(float(tp), digits)
        
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
        # Pré-check
        try:
            chk = self._mt5.order_check(req)
            if not chk or chk.retcode != 0:
                 code=chk.retcode if chk else -1; cmt=chk.comment if chk else "None"
                 self.log.error(f"Échec Pré-check Modif SLTP #{ticket}: Code={code}, Cmt={cmt}")
                 if chk and chk.retcode != TRADE_RETCODE_NO_MONEY: self.log.error(f"Détails: Marge Req={chk.margin:.2f}, Libre={chk.margin_free:.2f}")
                 return
        except Exception as e: self.log.error(f"Exception Pré-check Modif SLTP #{ticket}: {e}", exc_info=True); return
        # Envoi
        self.log.debug(f"Pré-check Modif SLTP #{ticket} OK. Envoi...")
        res = self._mt5.order_send(req)
        # Résultat
        if not res or res.retcode != mt5.TRADE_RETCODE_DONE:
            cmt=res.comment if res else "None"; code=res.retcode if res else "N/A"
            self.log.error(f"Échec ENVOI Modif SLTP #{ticket}: Code={code}, Cmt={cmt}")
            # ... Log détaillé si INVALID_STOPS ...
        else:
            self.log.info(f"Pos #{ticket} modifiée OK (SL: {sl:.{digits}f}, TP: {tp:.{digits}f}).")
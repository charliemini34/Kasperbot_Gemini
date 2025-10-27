# Fichier: src/risk/risk_manager.py
# Version: 18.1.0 (Fix R4 - Debug Log "No Money")
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, src.constants

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from typing import Tuple, List, Dict, Optional

from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque.
    v18.1.0: Ajout d'un log de débogage détaillé pour l'erreur "No Money" (R4).
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config
        self._executor = executor
        self._symbol: str = symbol
        
        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()
        
        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les informations pour le symbole {self._symbol} ou pour le compte.")
            raise ValueError("Informations de compte ou de symbole MT5 manquantes.")
            
        self.point: float = self.symbol_info.point
        self.digits: int = self.symbol_info.digits

    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        risk_settings = self._config.get('risk_management', {})
        loss_limit_percent = risk_settings.get('daily_loss_limit_percent', 2.0)
        if loss_limit_percent <= 0:
            return False, 0.0
        
        try:
            today_start_utc = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            history_deals = self._executor._mt5.history_deals_get(today_start_utc, datetime.utcnow())
            if history_deals is None: 
                self.log.warning("Impossible de récupérer l'historique des transactions pour la limite de perte journalière.")
                return False, 0.0

            magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
            
            daily_pnl = 0.0
            deals_by_position = {}
            for deal in history_deals:
                if deal.magic == magic_number:
                    if deal.position_id not in deals_by_position:
                        deals_by_position[deal.position_id] = []
                    deals_by_position[deal.position_id].append(deal)

            for position_id, deals in deals_by_position.items():
                if any(d.entry == mt5.DEAL_ENTRY_OUT or d.entry == mt5.DEAL_ENTRY_INOUT for d in deals):
                    daily_pnl += sum(d.profit for d in deals)
            
            loss_limit_amount = (self.account_info.equity * loss_limit_percent) / 100.0
            
            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) a atteint la limite de {loss_limit_percent}%.")
                return True, daily_pnl
                
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur critique dans le calcul de la limite de perte journalière : {e}", exc_info=True)
            return False, 0.0

    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal de trade invalide reçu: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0

            risk_percent = self._config.get('risk_management', {}).get('risk_per_trade', 0.01)
            
            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)
            
            if ideal_sl == 0 or ideal_tp == 0 or abs(price - ideal_sl) < self.symbol_info.point * 2:
                self.log.error(f"SL/TP invalide ou trop serré. SL: {ideal_sl}, TP: {ideal_tp}. Trade annulé.")
                return 0.0, 0.0, 0.0

            ideal_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)

            if ideal_volume < self.symbol_info.volume_min:
                self.log.warning(f"Le volume idéal ({ideal_volume:.4f}) est inférieur au min ({self.symbol_info.volume_min}). Trade annulé.")
                return 0.0, 0.0, 0.0
                
            return ideal_volume, ideal_sl, ideal_tp

        except Exception as e:
            self.log.error(f"Erreur inattendue lors du calcul des paramètres de trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0
    
    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        self.log.debug("--- DÉBUT DU CALCUL DE VOLUME SÉCURISÉ ---")
        
        # (R4) Ajout de logs de débogage pour 'No Money'
        try:
            leverage = self.account_info.leverage
            contract_size = self.symbol_info.trade_contract_size
            self.log.debug(f"Info Compte: Equity={equity:.2f} {self.account_info.currency}, Levier={leverage}:1")
            self.log.debug(f"Info Symbole ({self._symbol}): ContractSize={contract_size}, Devise Profit={self.symbol_info.currency_profit}")
        except Exception as e:
            self.log.warning(f"Erreur lors du log de débogage R4: {e}")
        # (Fin R4)

        risk_amount_account_currency = equity * risk_percent
        self.log.debug(f"1. Capital: {equity:.2f} | Risque: {risk_percent:.2%} -> Montant à risquer: {risk_amount_account_currency:.2f} {self.account_info.currency}")

        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point:
            self.log.error("Distance du SL quasi nulle. Annulation du calcul de volume.")
            return 0.0
        self.log.debug(f"2. Distance SL: {sl_distance_price:.{self.digits}f} (en prix de l'actif)")

        loss_per_lot_profit_currency = sl_distance_price * self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        self.log.debug(f"3. Perte/Lot en devise de profit ({profit_currency}): {loss_per_lot_profit_currency:.2f}")

        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if profit_currency != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(profit_currency, self.account_info.currency)
            if not conversion_rate or conversion_rate <= 0:
                self.log.error(f"Impossible d'obtenir un taux de conversion valide pour {profit_currency}->{self.account_info.currency}. Annulation.")
                return 0.0
            loss_per_lot_account_currency *= conversion_rate
            self.log.debug(f"4. Conversion: {profit_currency}/{self.account_info.currency} @ {conversion_rate:.5f} -> Perte/Lot en devise du compte: {loss_per_lot_account_currency:.2f}")
        else:
            self.log.debug("4. Pas de conversion de devise nécessaire.")

        if loss_per_lot_account_currency <= 0:
            self.log.error("La perte par lot calculée est nulle ou négative. Annulation.")
            return 0.0

        volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.debug(f"5. Volume brut: {risk_amount_account_currency:.2f} / {loss_per_lot_account_currency:.2f} = {volume:.4f} lots")

        volume_step = self.symbol_info.volume_step
        if volume_step > 0:
            volume = math.floor(volume / volume_step) * volume_step
        else:
             self.log.warning("Volume step is zero, cannot adjust volume.")
             return 0.0

        final_volume = max(0, min(self.symbol_info.volume_max, volume))

        self.log.debug(f"6. Volume final ajusté: {final_volume:.4f} (Min: {self.symbol_info.volume_min}, Max: {self.symbol_info.volume_max}, Step: {volume_step})")
        self.log.debug("--- FIN DU CALCUL DE VOLUME ---")
        return final_volume

    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        rm_settings = self._config.get('risk_management', {})
        strategy = rm_settings.get('sl_tp_strategy', 'ATR_MULTIPLE')
        direction = trade_signal['direction']

        atr_settings = rm_settings.get('atr_settings', {}).get('default', {})
        atr = self.calculate_atr(ohlc_data, atr_settings.get('period', 14))
        if atr is None or atr <= 0:
            self.log.error("ATR invalide. Impossible de calculer le SL.")
            return 0.0, 0.0
        
        sl_multiple = atr_settings.get('sl_multiple', 1.5)
        sl_distance = atr * sl_multiple
        sl = price - sl_distance if direction == BUY else price + sl_distance
        
        tp = 0.0
        if strategy == "SMC_LIQUIDITY_TARGET":
            tp = trade_signal.get('target_price')
            if not tp:
                self.log.error("Stratégie SMC choisie mais aucune cible de liquidité trouvée dans le signal.")
                return 0.0, 0.0
            self.log.debug(f"Stratégie SMC: Cible de liquidité à {tp:.{self.digits}f}")

        elif strategy == "ATR_MULTIPLE":
            tp_multiple = atr_settings.get('tp_multiple', 3.0)
            tp_distance = atr * tp_multiple
            tp = price + tp_distance if direction == BUY else price - tp_distance
            self.log.debug(f"Stratégie ATR: TP calculé à {tp:.{self.digits}f}")

        else:
            self.log.error(f"La stratégie SL/TP '{strategy}' n'est pas reconnue.")
            return 0.0, 0.0
        
        if abs(tp - price) < abs(sl - price):
            self.log.warning(f"Le TP est plus proche que le SL (Ratio < 1). Le trade pourrait ne pas être profitable.")

        return round(sl, self.digits), round(tp, self.digits)
        
    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        if from_currency == to_currency: return 1.0
        
        pair1 = f"{from_currency}{to_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.ask > 0:
            self.log.debug(f"Taux de change direct trouvé pour {pair1}: {info1.ask}")
            return info1.ask

        pair2 = f"{to_currency}{from_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.bid > 0:
            self.log.debug(f"Taux de change inverse trouvé pour {pair2}: {1.0 / info2.bid}")
            return 1.0 / info2.bid

        for pivot in ["USD", "EUR", "GBP"]:
             if from_currency != pivot and to_currency != pivot:
                 rate1 = self.get_conversion_rate(from_currency, pivot)
                 rate2 = self.get_conversion_rate(pivot, to_currency)
                 if rate1 and rate2:
                     cross_rate = rate1 * rate2
                     self.log.debug(f"Taux de change croisé trouvé via {pivot}: {cross_rate}")
                     return cross_rate

        self.log.error(f"Impossible de trouver une paire de conversion pour {from_currency} -> {to_currency}")
        return None
    
    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period:
            return None
        
        high_low = ohlc_data['high'] - ohlc_data['low']
        high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
        low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
        
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        
        return true_range.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    # (R1) Signature modifiée pour inclure trade_context
    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame, trade_context: dict):
        """Gère les positions ouvertes, y compris TP partiels, BE et Trailing."""
        
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return
        
        risk_settings = self._config.get('risk_management', {})
        
        if risk_settings.get('partial_tp', {}).get('enabled', False):
            self._apply_partial_tp(
                positions, 
                current_tick, 
                trade_context, 
                risk_settings.get('partial_tp', {})
            )
        
        if risk_settings.get('breakeven', {}).get('enabled', False):
            self._apply_breakeven(positions, current_tick, risk_settings.get('breakeven', {}))
            
        if risk_settings.get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, risk_settings)

    # (R1) Nouvelle fonction pour TP Partiels
    def _apply_partial_tp(self, positions: list, tick, trade_context: dict, partial_tp_cfg: dict):
        """Applique la logique de clôture partielle basée sur les niveaux de RR."""
        
        levels = sorted(partial_tp_cfg.get('levels', []), key=lambda x: x.get('rr', 0))
        magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
        
        if not levels: 
            return

        for pos in positions:
            context = trade_context.get(pos.ticket) 
            
            if not context:
                continue
                
            original_sl = context.get('original_sl')
            original_volume = context.get('original_volume')
            percent_already_closed = context.get('partial_tp_taken_percent', 0.0)
            
            if not original_sl or not original_volume or original_sl == 0 or original_volume == 0:
                self.log.warning(f"Contexte incomplet pour #{pos.ticket} (SL/Vol manquant). TP Partiel ignoré.")
                continue
            
            if percent_already_closed >= 0.999:
                continue
                
            sl_distance_price = abs(pos.price_open - original_sl)
            if sl_distance_price < self.point: continue
            
            current_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            profit_distance_price = 0.0
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                profit_distance_price = current_price - pos.price_open
            else: # SELL
                profit_distance_price = pos.price_open - current_price

            if profit_distance_price <= 0:
                continue

            current_rr = profit_distance_price / sl_distance_price
            
            target_level_config = None
            for level in reversed(levels):
                rr_target = level.get('rr', 0)
                percent_to_close_at_this_level = level.get('percent', 0) / 100.0 
                
                if current_rr >= rr_target and percent_already_closed < percent_to_close_at_this_level:
                    target_level_config = level
                    break
            
            if not target_level_config:
                continue
                
            total_percent_to_close = target_level_config.get('percent', 0) / 100.0
            percent_to_close_now = total_percent_to_close - percent_already_closed
            
            if percent_to_close_now <= 0.001:
                continue
            
            volume_to_close = original_volume * percent_to_close_now
            
            rr_label = target_level_config.get('rr')
            self.log.info(f"TP PARTIEL (R{rr_label}) déclenché pour #{pos.ticket} (RR actuel: {current_rr:.2f}). Clôture de {percent_to_close_now*100:.1f}% ({volume_to_close:.2f} lots).")
            
            result = self._executor.close_partial_position(pos.ticket, volume_to_close, magic_number, f"Partial TP R{rr_label}")
            
            if result:
                self._executor.update_trade_context_partials(pos.ticket, percent_to_close_now)

    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        trigger_pips = be_cfg.get('trigger_pips', 150)
        pips_plus = be_cfg.get('pips_plus', 10)
        
        for pos in positions:
            pnl_pips = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_pips = (tick.bid - pos.price_open) / self.point
                if pos.sl < pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open + (pips_plus * self.point)
                    if breakeven_sl > pos.sl:
                        self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f}")
                        self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_pips = (pos.price_open - tick.ask) / self.point
                if (pos.sl == 0 or pos.sl > pos.price_open) and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open - (pips_plus * self.point)
                    if (pos.sl == 0 or breakeven_sl < pos.sl):
                        self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f}")
                        self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
    
    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        ts_cfg = risk_cfg.get('trailing_stop_atr', {})
        atr_cfg = risk_cfg.get('atr_settings', {}).get('default', {})
        
        period = atr_cfg.get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0: return

        activation_multiple = ts_cfg.get('activation_multiple', 2.0)
        trailing_multiple = ts_cfg.get('trailing_multiple', 1.8)
        
        for pos in positions:
            new_sl = pos.sl
            current_profit = 0.0

            if pos.type == mt5.ORDER_TYPE_BUY:
                current_profit = (tick.bid - pos.price_open)
                if current_profit >= (atr * activation_multiple):
                    potential_new_sl = tick.bid - (atr * trailing_multiple)
                    if potential_new_sl > pos.sl:
                        new_sl = potential_new_sl
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_profit = (pos.price_open - tick.ask)
                if current_profit >= (atr * activation_multiple):
                    potential_new_sl = tick.ask + (atr * trailing_multiple)
                    if new_sl == 0 or potential_new_sl < new_sl:
                        new_sl = potential_new_sl
                        
            if new_sl != pos.sl:
                self.log.info(f"TRAILING STOP: Mise à jour du SL pour #{pos.ticket} à {new_sl:.{self.digits}f} (Profit: {current_profit:.2f}, ATR: {atr:.5f})")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)
# Fichier: src/risk/risk_manager.py
# Version: 13.0.2 (Stable Risk Engine)
# Dépendances: MetaTrader5, pandas, numpy, logging
# Description: Moteur de risque complet avec restauration des fonctions de sécurité critiques.

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime, time
import pytz
from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque des trades avec des garde-fous essentiels.
    v13.0.2 : Restauration de is_daily_loss_limit_reached et manage_open_positions.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self._config = config
        self._executor = executor
        self._symbol = symbol
        self.log = logging.getLogger(self.__class__.__name__)
        
        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()
        
        if not self.symbol_info or not self.account_info:
            raise ValueError("Impossible d'obtenir les infos du symbole ou du compte.")
            
        self.point = self.symbol_info.point
        self.digits = self.symbol_info.digits

    def is_daily_loss_limit_reached(self) -> (bool, float):
        """Vérifie si la limite de perte journalière est atteinte."""
        loss_limit_percent = self._config.get('risk_management', {}).get('daily_loss_limit_percent', 2.0)
        if loss_limit_percent <= 0: return False, 0.0
        try:
            broker_tz = pytz.timezone("EET")
            now_in_broker_tz = datetime.now(broker_tz)
            today_start_broker_tz = now_in_broker_tz.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start_broker_tz.astimezone(pytz.utc)
            now_utc = datetime.now(pytz.utc)
            history_deals = self._executor._mt5.history_deals_get(today_start_utc, now_utc)
            if history_deals is None: return False, 0.0
            daily_pnl = sum(deal.profit for deal in history_deals)
            loss_limit_amount = (self.account_info.equity * loss_limit_percent) / 100
            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) a atteint la limite de {loss_limit_percent}%.")
                return True, daily_pnl
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur dans le calcul de la limite de perte journalière : {e}")
            return False, 0.0

    def calculate_trade_parameters(self, equity: float, price: float, direction: str, ohlc_data):
        risk_settings = self._config.get('risk_management', {})
        risk_percent = risk_settings.get('risk_per_trade', 0.01)
        adjust_sl = risk_settings.get('adjust_sl_to_fit_risk', False)
        ideal_sl, ideal_tp, atr_multiple_tp = self._calculate_initial_sl_tp(price, direction, ohlc_data, self._symbol)
        ideal_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)

        if ideal_volume < self.symbol_info.volume_min:
            if adjust_sl:
                self.log.warning(f"Volume idéal ({ideal_volume:.4f}) < min ({self.symbol_info.volume_min}). Réajustement du SL...")
                final_volume = self.symbol_info.volume_min
                risk_amount = equity * risk_percent
                loss_per_lot_per_point = self.symbol_info.trade_contract_size * self.point
                if self.symbol_info.currency_profit != self.account_info.currency:
                    conversion_rate = self.get_conversion_rate(self.symbol_info.currency_profit, self.account_info.currency)
                    if conversion_rate and conversion_rate > 0: loss_per_lot_per_point /= conversion_rate
                    else: return 0.0, 0.0, 0.0
                new_sl_distance_points = (risk_amount / final_volume) / loss_per_lot_per_point
                new_sl_distance_price = new_sl_distance_points * self.point
                if direction == BUY:
                    final_sl = price - new_sl_distance_price
                    final_tp = price + (new_sl_distance_price * atr_multiple_tp)
                else:
                    final_sl = price + new_sl_distance_price
                    final_tp = price - (new_sl_distance_price * atr_multiple_tp)
                self.log.info(f"Nouveau SL ajusté: {final_sl:.{self.digits}f} (Distance: {new_sl_distance_price:.{self.digits}f})")
                return final_volume, round(final_sl, self.digits), round(final_tp, self.digits)
            else:
                self.log.warning(f"RISQUE NON RESPECTÉ: Volume calculé ({ideal_volume:.4f}) < min ({self.symbol_info.volume_min}). Trade annulé.")
                return 0.0, 0.0, 0.0
        return ideal_volume, ideal_sl, ideal_tp

    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        risk_amount_account_currency = equity * risk_percent
        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price == 0: return 0.0
        loss_per_lot_profit_currency = sl_distance_price * self.symbol_info.trade_contract_size
        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if self.symbol_info.currency_profit != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(self.symbol_info.currency_profit, self.account_info.currency)
            if not conversion_rate or conversion_rate == 0: return 0.0
            loss_per_lot_account_currency /= conversion_rate
        if loss_per_lot_account_currency <= 0: return 0.0
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        volume_step = self.symbol_info.volume_step
        if volume_step > 0: volume = math.floor(volume / volume_step) * volume_step
        return round(min(self.symbol_info.volume_max, volume), 2)

    def _calculate_initial_sl_tp(self, price: float, direction: str, ohlc_data, symbol: str):
        rm_settings = self._config.get('risk_management', {})
        strategy = rm_settings.get('sl_tp_strategy', 'ATR_MULTIPLE')
        tick_info = self._executor._mt5.symbol_info_tick(symbol)
        spread = (tick_info.ask - tick_info.bid) if tick_info else 0
        tp_multiple = rm_settings.get('atr_settings', {}).get('default', {}).get('tp_multiple', 3.0)
        if strategy == "ATR_MULTIPLE":
            symbol_settings = rm_settings.get('atr_settings', {}).get(symbol, rm_settings.get('atr_settings', {}).get('default', {}))
            period = symbol_settings.get('period', 14)
            atr = self.calculate_atr(ohlc_data, period)
            sl_multiple = symbol_settings.get('sl_multiple', 1.5)
            tp_multiple = symbol_settings.get('tp_multiple', 3.0)
            if direction == BUY:
                sl = price - (atr * sl_multiple) - spread
                tp = price + (atr * tp_multiple)
            else:
                sl = price + (atr * sl_multiple) + spread
                tp = price - (atr * tp_multiple)
            return round(sl, self.digits), round(tp, self.digits), tp_multiple
        return price, price, 1.0

    def get_conversion_rate(self, from_currency: str, to_currency: str) -> float | None:
        if from_currency == to_currency: return 1.0
        pair1 = f"{to_currency}{from_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.bid > 0: return info1.bid
        pair2 = f"{from_currency}{to_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.ask > 0: return 1.0 / info2.ask
        self.log.error(f"Impossible de trouver un taux de conversion pour {from_currency}->{to_currency}")
        return None
    
    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> float:
        if ohlc_data is None or ohlc_data.empty: return 0.0
        high_low = ohlc_data['high'] - ohlc_data['low']
        high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
        low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return
        self._apply_breakeven(positions, current_tick)
        if self._config.get('risk_management', {}).get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data)

    def _apply_breakeven(self, positions, tick):
        cfg = self._config.get('risk_management', {}).get('breakeven', {})
        if not cfg.get('enabled', False): return
        trigger_distance = cfg.get('trigger_pips', 150) * 10 * self.point
        pips_plus = cfg.get('pips_plus', 10) * 10 * self.point
        for pos in positions:
            if pos.type == mt5.ORDER_TYPE_BUY:
                breakeven_sl = pos.price_open + pips_plus
                if pos.sl < pos.price_open and (tick.bid - pos.price_open) >= trigger_distance:
                    self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                breakeven_sl = pos.price_open - pips_plus
                if (pos.sl == 0 or pos.sl > pos.price_open) and (pos.price_open - tick.ask) >= trigger_distance:
                    self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
    
    def _apply_trailing_stop_atr(self, positions, tick, ohlc_data):
        cfg = self._config.get('risk_management', {}).get('trailing_stop_atr', {})
        atr_settings = self._config.get('risk_management', {}).get('atr_settings', {})
        period = atr_settings.get('default', {}).get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        activation_multiple = cfg.get('activation_multiple', 2.0)
        trailing_multiple = cfg.get('trailing_multiple', 1.8)
        activation_distance = atr * activation_multiple
        trailing_distance = atr * trailing_multiple
        for pos in positions:
            new_sl = pos.sl
            if pos.type == mt5.ORDER_TYPE_BUY:
                if (tick.bid - pos.price_open) >= activation_distance:
                    potential_new_sl = tick.bid - trailing_distance
                    if potential_new_sl > pos.sl: new_sl = potential_new_sl
            elif pos.type == mt5.ORDER_TYPE_SELL:
                if (pos.price_open - tick.ask) >= activation_distance:
                    potential_new_sl = tick.ask + trailing_distance
                    if new_sl == 0 or potential_new_sl < new_sl: new_sl = potential_new_sl
            if new_sl != pos.sl:
                self.log.info(f"TRAILING STOP: Mise à jour du SL pour #{pos.ticket} à {new_sl:.{self.digits}f}")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)
# Fichier: src/risk/risk_manager.py

import MetaTrader5 as mt5
import logging
import math
import pandas as pd  # <-- CORRECTION : Import manquant
import numpy as np   # <-- CORRECTION : Import manquant

class RiskManager:
    """
    Gère le risque des trades.
    v7.5 : Correction du bug NameError.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self._config = config
        self._executor = executor
        self._symbol = symbol
        self.log = logging.getLogger(self.__class__.__name__)
        
        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()
        
        if not self.symbol_info or not self.account_info:
            self.log.critical("Impossible d'obtenir les infos du symbole ou du compte.")
            raise ValueError("Erreur d'initialisation du RiskManager.")
            
        self.point = self.symbol_info.point

    def calculate_sl_tp(self, price: float, direction: str, ohlc_data):
        """Calcule les prix SL et TP en se basant sur la stratégie définie dans la config."""
        strategy = self._config.get('sl_tp_strategy', 'FIXED_PIPS')
        if strategy == "ATR_MULTIPLE":
            return self._calculate_sl_tp_atr(price, direction, ohlc_data)
        else:
            sl_pips = self._config.get('fixed_pips_settings', {}).get('stop_loss_pips', 150)
            tp_pips = self._config.get('fixed_pips_settings', {}).get('take_profit_pips', 400)
            sl_distance = sl_pips * 10 * self.point
            tp_distance = tp_pips * 10 * self.point
            if direction == "BUY":
                sl, tp = price - sl_distance, price + tp_distance
            else:
                sl, tp = price + sl_distance, price - tp_distance
            return round(sl, self.symbol_info.digits), round(tp, self.symbol_info.digits)

    def _calculate_sl_tp_atr(self, price: float, direction: str, ohlc_data):
        """Calcule SL/TP en utilisant un multiple de l'ATR."""
        settings = self._config.get('atr_settings', {})
        period, sl_multiple, tp_multiple = settings.get('period', 14), settings.get('sl_multiple', 2.0), settings.get('tp_multiple', 4.0)

        high_low = ohlc_data['high'] - ohlc_data['low']
        high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
        low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        atr = true_range.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
        
        if direction == "BUY":
            sl, tp = price - (atr * sl_multiple), price + (atr * tp_multiple)
        else:
            sl, tp = price + (atr * sl_multiple), price - (atr * tp_multiple)
        return round(sl, self.symbol_info.digits), round(tp, self.symbol_info.digits)

    # ... Le reste du fichier est stable et reste inchangé ...
    def calculate_volume(self, equity: float, entry_price: float, sl_price: float) -> float:
        risk_percent = self._config.get('risk_per_trade', 0.01)
        risk_amount_account_currency = equity * risk_percent
        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point * 10: return 0.0

        contract_size = self.symbol_info.trade_contract_size
        loss_per_lot_profit_currency = sl_distance_price * contract_size
        account_currency = self.account_info.currency
        loss_per_lot_account_currency = loss_per_lot_profit_currency

        if self.symbol_info.currency_profit != account_currency:
            conversion_rate = self.get_conversion_rate(self.symbol_info.currency_profit, account_currency)
            if not conversion_rate or conversion_rate == 0: return 0.0
            loss_per_lot_account_currency /= conversion_rate

        if loss_per_lot_account_currency <= 0: return 0.0
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        
        volume_step = self.symbol_info.volume_step
        volume = math.floor(volume / volume_step) * volume_step
        return round(max(self.symbol_info.volume_min, min(self.symbol_info.volume_max, volume)), 2)

    def get_conversion_rate(self, from_currency: str, to_currency: str) -> float | None:
        pair1 = f"{to_currency}{from_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.bid > 0: return info1.bid
        pair2 = f"{from_currency}{to_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.ask > 0: return 1.0 / info2.ask
        return None

    def is_daily_loss_limit_reached(self, equity: float, daily_pnl: float) -> bool:
        loss_limit_percent = self._config.get('daily_loss_limit_percent', 0.05)
        loss_limit_amount = equity * loss_limit_percent
        return daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount

    def manage_open_positions(self, positions: list, current_tick):
        if not positions or not current_tick: return
        self._apply_breakeven(positions, current_tick)
        self._apply_trailing_stop(positions, current_tick)

    def _apply_breakeven(self, positions, tick):
        cfg = self._config.get('breakeven', {})
        if not cfg.get('enabled', False): return
        trigger_distance = cfg.get('trigger_pips', 150) * 10 * self.point
        pips_plus = cfg.get('pips_plus', 10) * 10 * self.point
        for pos in positions:
            if pos.type == mt5.ORDER_TYPE_BUY:
                breakeven_sl = pos.price_open + pips_plus
                if pos.sl < pos.price_open and (tick.bid - pos.price_open) >= trigger_distance:
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                breakeven_sl = pos.price_open - pips_plus
                if (pos.sl == 0 or pos.sl > pos.price_open) and (pos.price_open - tick.ask) >= trigger_distance:
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)

    def _apply_trailing_stop(self, positions, tick):
        cfg = self._config.get('trailing_stop', {})
        if not cfg.get('enabled', False): return
        activation_distance = cfg.get('activation_pips', 250) * 10 * self.point
        trailing_distance = cfg.get('trailing_pips', 200) * 10 * self.point
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
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)
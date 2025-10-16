# Fichier: src/risk/risk_manager.py
# Version Finale Robuste par votre Partenaire de Code

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque des trades de manière professionnelle et sécurisée.
    v10.0 : Annule le trade si le volume calculé est inférieur au minimum autorisé
            pour garantir un respect strict du risque défini par l'utilisateur.
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

    def check_risk_reward_ratio(self, entry_price: float, sl_price: float, tp_price: float) -> bool:
        """Vérifie si le ratio Risque/Rendement du trade potentiel est acceptable."""
        min_rr_ratio = self._config.get('risk_management', {}).get('min_risk_reward_ratio', 2.0)

        potential_loss = abs(entry_price - sl_price)
        potential_profit = abs(tp_price - entry_price)

        if potential_loss < self.point * 10: # Évite la division par zéro et les SL trop serrés
            return False

        rr_ratio = potential_profit / potential_loss
        
        if rr_ratio >= min_rr_ratio:
            self.log.info(f"Ratio R/R ({rr_ratio:.2f}) >= Seuil ({min_rr_ratio}). Trade autorisé.")
            return True
        else:
            self.log.warning(f"Ratio R/R ({rr_ratio:.2f}) < Seuil ({min_rr_ratio}). Trade refusé.")
            return False

    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> float:
        """Calcule la valeur de l'Average True Range (ATR)."""
        if ohlc_data is None or ohlc_data.empty: return 0.0
        high_low = ohlc_data['high'] - ohlc_data['low']
        high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
        low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.ewm(alpha=1/period, adjust=False).mean().iloc[-1]

    def calculate_volume(self, equity: float, entry_price: float, sl_price: float) -> float:
        """Calcule le volume en respectant scrupuleusement le risque défini."""
        self.log.info("--- Début du Calcul de Volume ---")
        risk_settings = self._config.get('risk_management', {})
        risk_percent = risk_settings.get('risk_per_trade', 0.01)
        risk_amount_account_currency = equity * risk_percent
        self.log.info(f"1. Capital: {equity:.2f} | Risque %: {risk_percent*100:.2f}% -> Montant à risquer: {risk_amount_account_currency:.2f} {self.account_info.currency}")
        
        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point * 5: 
            self.log.warning("Distance de SL trop faible, calcul de volume annulé.")
            return 0.0
        self.log.info(f"2. Distance du SL en prix: {sl_distance_price:.{self.digits}f}")

        contract_size = self.symbol_info.trade_contract_size
        loss_per_lot_profit_currency = sl_distance_price * contract_size
        self.log.info(f"3. Perte pour 1 lot: {loss_per_lot_profit_currency:.2f} {self.symbol_info.currency_profit}")

        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if self.symbol_info.currency_profit != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(self.symbol_info.currency_profit, self.account_info.currency)
            if not conversion_rate or conversion_rate == 0: 
                self.log.error("Taux de conversion introuvable. Ordre annulé.")
                return 0.0
            loss_per_lot_account_currency /= conversion_rate
            self.log.info(f"4. Taux de change {self.symbol_info.currency_profit}->{self.account_info.currency}: {conversion_rate:.5f} | Perte/lot convertie: {loss_per_lot_account_currency:.2f} {self.account_info.currency}")

        if loss_per_lot_account_currency <= 0: return 0.0
        
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.info(f"5. Volume brut calculé: {volume:.4f} lots")

        # --- LOGIQUE DE SÉCURITÉ CRITIQUE ---
        if volume < self.symbol_info.volume_min:
            self.log.warning(f"RISQUE NON RESPECTÉ: Le volume calculé ({volume:.4f}) est inférieur au minimum du broker ({self.symbol_info.volume_min}). Trade annulé pour ne pas dépasser le risque.")
            return 0.0

        volume_step = self.symbol_info.volume_step
        if volume_step > 0:
            volume = math.floor(volume / volume_step) * volume_step
        
        final_volume = round(min(self.symbol_info.volume_max, volume), 2)
        self.log.info(f"6. Volume final sécurisé: {final_volume:.2f} lots")
        self.log.info("--- Fin du Calcul de Volume ---")
        return final_volume
        
    def calculate_sl_tp(self, price: float, direction: str, ohlc_data, symbol: str):
        """Calcule SL/TP en intégrant le spread pour plus de précision."""
        rm_settings = self._config.get('risk_management', {})
        strategy = rm_settings.get('sl_tp_strategy', 'ATR_MULTIPLE')
        
        tick_info = self._executor._mt5.symbol_info_tick(symbol)
        spread = (tick_info.ask - tick_info.bid) if tick_info else 0

        if strategy == "ATR_MULTIPLE":
            symbol_settings = rm_settings.get('atr_settings', {}).get(symbol, rm_settings.get('atr_settings', {}).get('default', {}))
            period = symbol_settings.get('period', 14)
            atr = self.calculate_atr(ohlc_data, period)
            sl_multiple = symbol_settings.get('sl_multiple', 1.5)
            tp_multiple = symbol_settings.get('tp_multiple', 3.0)
            
            if direction == BUY:
                sl = price - (atr * sl_multiple) - spread
                tp = price + (atr * tp_multiple)
            else: # SELL
                sl = price + (atr * sl_multiple) + spread
                tp = price - (atr * tp_multiple)
            return round(sl, self.digits), round(tp, self.digits)
        else: # FIXED_PIPS (non recommandé mais disponible)
            fixed_settings = rm_settings.get('fixed_pips_settings', {})
            sl_pips = fixed_settings.get('stop_loss_pips', 150)
            tp_pips = fixed_settings.get('take_profit_pips', 400)
            sl_distance = sl_pips * 10 * self.point
            tp_distance = tp_pips * 10 * self.point

            if direction == BUY:
                sl, tp = price - sl_distance - spread, price + tp_distance
            else: # SELL
                sl, tp = price + sl_distance + spread, price - tp_distance
            return round(sl, self.digits), round(tp, self.digits)

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

    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return
        self._apply_breakeven(positions, current_tick)
        if self._config.get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data)

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
    
    def _apply_trailing_stop_atr(self, positions, tick, ohlc_data):
        cfg = self._config.get('trailing_stop_atr', {})
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
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)
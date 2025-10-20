# Fichier: src/risk/risk_manager.py
# Version: 18.1.8 (Critical-Fixes-Attr-Const)
# Dépendances: MetaTrader5, pandas, numpy, logging, decimal, pytz, datetime, typing
# Description: Corrige AttributeError 'tick_value' et NameError 'SYMBOL_CALC_MODE_FX'.

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, time as dt_time, timedelta
import pytz
from typing import Tuple, Optional, Dict, List, TYPE_CHECKING

from src.constants import BUY, SELL

if TYPE_CHECKING:
    from src.execution.mt5_executor import MT5Executor

class RiskManager:
    """
    Gère le risque.
    v18.1.8: Correction 'tick_value' -> 'trade_tick_value' ET 'SYMBOL_CALC_MODE_FX' -> 'SYMBOL_CALC_MODE_FOREX'.
    """

    def __init__(self, config: dict, executor: 'MT5Executor', symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self.executor = executor
        try:
            self.mt5 = executor.get_mt5_connection()
        except AttributeError:
             # Fallback si l'executor (v15.4.7 fourni) n'a pas encore le getter
             self.log.warning("Utilisation de l'accès direct .mt5. Mettre à jour MT5Executor (v15.5.0+).")
             self.mt5 = executor.mt5 
        
        self.symbol = symbol
        self.symbol_info = self.mt5.symbol_info(self.symbol)
        self.account_info = self.executor.get_account_info()

        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les infos symbole/compte pour {self.symbol}.")
            raise ValueError(f"Infos symbole MT5 introuvables pour {self.symbol}")

        # Paramètres de risque
        risk_settings = self.config.get('risk_management', {})
        self.risk_per_trade_pct = Decimal(str(risk_settings.get('risk_per_trade', 1.0))) / Decimal('100.0')
        self.daily_loss_limit_pct = Decimal(str(risk_settings.get('daily_loss_limit_percent', 5.0))) / Decimal('100.0')
        self.max_concurrent_risk_pct = Decimal(str(risk_settings.get('max_concurrent_risk_percent', 3.0))) / Decimal('100.0')

        # Paramètres de gestion
        management_settings = self.config.get('risk_management', {}) # Corrigé : 'trade_management' n'existe pas dans config v18.3.1
        self.ptp_rules = management_settings.get('partial_tp', {}).get('levels', [])
        self.breakeven_rules = management_settings.get('breakeven', {})
        self.trailing_stop_rules = management_settings.get('trailing_stop_atr', {})

        # Paramètres SL/TP
        sl_tp_settings = self.config.get('risk_management', {})
        self.sl_strategy = sl_tp_settings.get('sl_strategy', 'ATR_MULTIPLE')
        self.tp_strategy = sl_tp_settings.get('tp_strategy', 'SMC_LIQUIDITY_TARGET')
        self.sl_buffer_pips = Decimal(str(sl_tp_settings.get('sl_buffer_pips', 1.0)))
        self.tp_buffer_pips = Decimal(str(sl_tp_settings.get('tp_buffer_pips', 0.0)))
        
        atr_settings = sl_tp_settings.get('atr_settings', {})
        symbol_atr_override = atr_settings.get(symbol, {})
        default_atr = atr_settings.get('default', {'sl_multiple': 2.0, 'tp_multiple': 3.0})
        
        self.sl_atr_multiplier = Decimal(str(symbol_atr_override.get('sl_multiple', default_atr.get('sl_multiple', 2.0))))
        self.tp_atr_multiplier = Decimal(str(symbol_atr_override.get('tp_multiple', default_atr.get('tp_multiple', 3.0))))

        # Propriétés du symbole
        self.digits = self.symbol_info.digits
        self.point = self.symbol_info.point
        self.volume_min = self.symbol_info.volume_min
        self.volume_max = self.symbol_info.volume_max
        self.volume_step = self.symbol_info.volume_step
        self.trade_contract_size = self.symbol_info.trade_contract_size
        self.currency_profit = self.symbol_info.currency_profit
        self.currency_margin = self.symbol_info.currency_margin
        self.trade_calc_mode = self.symbol_info.trade_calc_mode
        
        # --- CORRECTION : Utiliser les bons attributs ---
        self.trade_tick_value = self.symbol_info.trade_tick_value
        self.trade_tick_size = self.symbol_info.trade_tick_size
        # --- FIN CORRECTION ---

        self.account_currency = self.account_info.currency
        self.ohlc_data_cache = None

    def _calculate_atr(self, ohlc_data: pd.DataFrame, period: int = 14) -> float:
        # ... (inchangé) ...
        if ohlc_data is None or len(ohlc_data) < period:
            logging.warning(f"Données OHLC insuffisantes pour ATR({period}) sur {self.symbol}")
            return 0.0
        # Assurer que les colonnes sont numériques
        ohlc_data['high'] = pd.to_numeric(ohlc_data['high'], errors='coerce')
        ohlc_data['low'] = pd.to_numeric(ohlc_data['low'], errors='coerce')
        ohlc_data['close'] = pd.to_numeric(ohlc_data['close'], errors='coerce')
        ohlc_data = ohlc_data.dropna(subset=['high', 'low', 'close'])
        if len(ohlc_data) < period: return 0.0

        high_low = ohlc_data['high'] - ohlc_data['low']
        high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
        low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        
        if pd.isna(atr) or atr == 0.0:
             logging.warning(f"Calcul ATR invalide (NaN ou 0.0) pour {self.symbol}. Fallback 10 pips.")
             return self.point * 10
        
        return atr


    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Decimal:
        # ... (inchangé) ...
        if from_currency == to_currency: return Decimal('1.0')
        ticker_direct = f"{from_currency}{to_currency}"
        tick_direct = self.mt5.symbol_info_tick(ticker_direct)
        if tick_direct and tick_direct.ask > 0:
            return Decimal(str(tick_direct.ask))
        ticker_inverse = f"{to_currency}{from_currency}"
        tick_inverse = self.mt5.symbol_info_tick(ticker_inverse)
        if tick_inverse and tick_inverse.bid > 0:
            try: return Decimal('1.0') / Decimal(str(tick_inverse.bid))
            except (InvalidOperation, ZeroDivisionError): pass
        if from_currency != "USD" and to_currency != "USD":
            try:
                rate_from_usd = self.get_conversion_rate(from_currency, "USD")
                rate_usd_to = self.get_conversion_rate("USD", to_currency)
                if rate_from_usd > 0 and rate_usd_to > 0: return rate_from_usd * rate_usd_to
            except Exception as e: logging.error(f"Erreur taux croisé {from_currency}{to_currency}: {e}")
        logging.error(f"Impossible d'obtenir taux conversion {from_currency} -> {to_currency}.")
        return Decimal('0.0')


    def _calculate_pip_value_in_account_currency(self, volume: Decimal = Decimal('1.0')) -> Decimal:
        """ Calcule la valeur d'un point (PAS un pip) pour un volume donné. """
        try:
            point_d = Decimal(str(self.point))
            contract_size_d = Decimal(str(self.trade_contract_size))
            pip_value_profit_currency = Decimal('0.0')
            calc_mode = self.trade_calc_mode
            
            # --- CORRECTION : Utiliser les attributs de classe ---
            tick_value_d = Decimal(str(self.trade_tick_value))
            tick_size_d = Decimal(str(self.trade_tick_size))
            # --- FIN CORRECTION ---

            # --- CORRECTION : Utiliser la constante MT5 correcte ---
            if calc_mode == mt5.SYMBOL_CALC_MODE_FOREX: 
            # --- FIN CORRECTION ---
                 # Pour Forex, 1 point = Point * ContractSize (en devise de cotation)
                 pip_value_profit_currency = point_d * contract_size_d

            elif calc_mode in [mt5.SYMBOL_CALC_MODE_CFD, mt5.SYMBOL_CALC_MODE_CFDINDEX, mt5.SYMBOL_CALC_MODE_FUTURES, mt5.SYMBOL_CALC_MODE_CFDLEVERAGE]:
                 if tick_size_d == 0: logging.error(f"{self.symbol}: Tick size est zéro."); return Decimal('0.0')
                 # Valeur d'un point = (point / tick_size) * tick_value
                 # La valeur du tick (tick_value) est généralement en devise de MARGE (pas profit)
                 point_value_margin_curr = (point_d / tick_size_d) * tick_value_d * volume
                 
                 if self.currency_margin == self.account_currency:
                      pip_value_profit_currency = point_value_margin_curr # Pas besoin de convertir
                 else:
                      # Convertir de la devise de marge à la devise du compte
                      conversion_rate = self.get_conversion_rate(self.currency_margin, self.account_currency)
                      if conversion_rate <= 0: logging.error(f"Taux conversion invalide {self.currency_margin}->{self.account_currency}"); return Decimal('0.0')
                      pip_value_profit_currency = point_value_margin_curr * conversion_rate
            
            else:
                 logging.warning(f"Mode calcul {calc_mode} non géré. Fallback simple.")
                 pip_value_profit_currency = point_d * contract_size_d # Fallback
            
            # Appliquer volume (si non déjà fait)
            if calc_mode not in [mt5.SYMBOL_CALC_MODE_CFD, mt5.SYMBOL_CALC_MODE_CFDINDEX, mt5.SYMBOL_CALC_MODE_FUTURES, mt5.SYMBOL_CALC_MODE_CFDLEVERAGE]:
                 pip_value_profit_currency *= volume

            # Convertir devise de profit (si Forex/Fallback) en devise compte
            if calc_mode not in [mt5.SYMBOL_CALC_MODE_CFD, mt5.SYMBOL_CALC_MODE_CFDINDEX, mt5.SYMBOL_CALC_MODE_FUTURES, mt5.SYMBOL_CALC_MODE_CFDLEVERAGE]:
                if self.currency_profit != self.account_currency:
                    conversion_rate = self.get_conversion_rate(self.currency_profit, self.account_currency)
                    if conversion_rate > 0:
                        pip_value_profit_currency = pip_value_profit_currency * conversion_rate
                    else:
                        logging.error(f"Pip Value: Taux conversion {self.currency_profit}->{self.account_currency} invalide.")
                        return Decimal('0.0')
            
            return pip_value_profit_currency.quantize(Decimal("0.00001")) # Plus de précision

        except Exception as e:
            self.log.error(f"Erreur inattendue dans _calculate_pip_value: {e}", exc_info=True)
            return Decimal('0.0')


    def _calculate_volume(self, equity: Decimal, sl_price: Decimal, entry_price: Decimal, direction: str) -> Decimal:
        # ... (inchangé, mais logs mis à jour) ...
        log_entries = [f"Calcul Volume {self.symbol}:"]
        try:
            log_entries.append(f"  1. Equity: {equity:.2f} {self.account_currency}")
            risk_amount = equity * self.risk_per_trade_pct
            log_entries.append(f"  2. Risque Config: {self.risk_per_trade_pct * 100:.2f}%")
            log_entries.append(f"  3. Montant Risqué: {risk_amount:.2f} {self.account_currency}")

            if direction == "BUY": sl_distance_price = entry_price - sl_price
            else: sl_distance_price = sl_price - entry_price
            
            if sl_distance_price <= 0:
                log_entries.append(f"  ERREUR: Distance SL <= 0 ({sl_distance_price}).")
                logging.info("\n".join(log_entries)); return Decimal('0.0')

            log_entries.append(f"  4. Entrée={entry_price}, SL={sl_price}")
            log_entries.append(f"  5. Distance SL (Prix): {sl_distance_price}")

            point_value_1_lot = self._calculate_pip_value_in_account_currency(Decimal('1.0'))
            if point_value_1_lot is None or point_value_1_lot <= 0:
                 log_entries.append(f"  ERREUR: Valeur Point (1 lot) invalide: {point_value_1_lot}")
                 logging.info("\n".join(log_entries)); return Decimal('0.0')
            log_entries.append(f"  6. Valeur Point (1 Lot): {point_value_1_lot:.6f} {self.account_currency}")

            sl_distance_in_points = sl_distance_price / Decimal(str(self.point))
            loss_per_lot = sl_distance_in_points * point_value_1_lot
            log_entries.append(f"  7. Distance (Points): {sl_distance_in_points:.1f}")
            log_entries.append(f"  8. Perte pour 1 Lot: {loss_per_lot:.2f} {self.account_currency}")

            if loss_per_lot <= 0:
                log_entries.append(f"  ERREUR: Perte par lot invalide: {loss_per_lot}");
                logging.info("\n".join(log_entries)); return Decimal('0.0')

            volume = risk_amount / loss_per_lot
            log_entries.append(f"  9. Volume (Brut): {volume:.8f} lots")

            step = Decimal(str(self.volume_step))
            if step <= 0:
                log_entries.append("  ERREUR: Volume step invalide.");
                logging.info("\n".join(log_entries)); return Decimal('0.0')
            
            volume_rounded = (volume / step).to_integral_value(rounding=ROUND_DOWN) * step
            log_entries.append(f"  10. Volume (Arrondi Step {step}): {volume_rounded} lots")

            volume_min_d = Decimal(str(self.volume_min))
            volume_max_d = Decimal(str(self.volume_max))
            
            if volume_rounded < volume_min_d:
                loss_at_min_volume = (volume_min_d / Decimal('1.0')) * loss_per_lot
                if loss_at_min_volume > (risk_amount * Decimal('1.5')): # Tolérance 50%
                     logging.critical(f"RISQUE ÉLEVÉ: Vol Min {volume_min_d} = Risque {loss_at_min_volume:.2f} (Limite {risk_amount:.2f}). Trade annulé.")
                     log_entries.append(f"  ERREUR: Vol Min {volume_min_d} = Risque {loss_at_min_volume:.2f} > Limite")
                     volume_final = Decimal('0.0')
                else:
                     volume_final = volume_min_d
                     log_entries.append(f"  11. Ajusté à Vol Min: {volume_final} lots (Risque réel: {loss_at_min_volume:.2f})")
            elif volume_rounded > volume_max_d:
                volume_final = volume_max_d
                loss_at_max_volume = (volume_final / Decimal('1.0')) * loss_per_lot
                log_entries.append(f"  11. Ajusté à Vol Max: {volume_final} lots (Risque réel: {loss_at_max_volume:.2f})")
            else:
                volume_final = volume_rounded
                if volume_final > 0: # Ne pas logger si 0
                    loss_at_final_volume = (volume_final / Decimal('1.0')) * loss_per_lot
                    log_entries.append(f"  11. Volume Final: {volume_final} lots (Risque réel: {loss_at_final_volume:.2f})")
                else:
                    log_entries.append(f"  11. Volume Final: 0.0 lots")
            
            logging.info("\n".join(log_entries))
            return volume_final.quantize(step) # Assurer format final

        except InvalidOperation as e:
            logging.error(f"Erreur Decimal (InvalidOperation) dans _calculate_volume: {e}.")
            log_entries.append(f"  ERREUR (InvalidOperation): {e}")
            logging.info("\n".join(log_entries)); return Decimal('0.0')
        except Exception as e:
            logging.error(f"Erreur inattendue dans _calculate_volume: {e}", exc_info=True)
            log_entries.append(f"  ERREUR (Inattendue): {e}")
            logging.info("\n".join(log_entries)); return Decimal('0.0')


    def _calculate_sl_tp_levels(self, entry_price: float, direction: str, ohlc_data: pd.DataFrame, trade_signal: dict) -> tuple:
        sl_price = 0.0; tp_price = 0.0
        atr = self._calculate_atr(ohlc_data, 14)
        
        # 1. Calcul SL
        sl_calculated_structurally = False
        if self.sl_strategy == 'SMC_STRUCTURE' and trade_signal.get('sl_price', 0.0) > 0:
            sl_structure_price = trade_signal['sl_price']
            sl_buffer_abs = float(self.sl_buffer_pips) * self.point
            potential_sl = sl_structure_price - sl_buffer_abs if direction == "BUY" else sl_structure_price + sl_buffer_abs
            
            if atr > 0 and abs(entry_price - potential_sl) > (atr * 0.5):
                # Utiliser Decimal pour le calcul temp
                temp_vol_d = self._calculate_volume(Decimal(str(self.account_info.equity)), Decimal(str(potential_sl)), Decimal(str(entry_price)), direction)
                if temp_vol_d is not None and temp_vol_d >= Decimal(str(self.volume_min)):
                    sl_price = potential_sl; sl_calculated_structurally = True
                    logging.debug(f"SL {self.symbol} (SMC_STRUCTURE): {sl_price:.{self.digits}f}")
                else:
                    logging.warning(f"SL Structurel {self.symbol} ({potential_sl}) donne volume {temp_vol_d} < min. Fallback ATR.")
            else:
                 logging.warning(f"SL Structurel {self.symbol} ({potential_sl}) trop proche/ATR invalide. Fallback ATR.")
                 
        if not sl_calculated_structurally:
            if atr == 0.0: logging.error("ATR invalide, impossible de calculer SL fallback."); return 0.0, 0.0
            sl_distance = float(self.sl_atr_multiplier) * atr
            sl_price = entry_price - sl_distance if direction == "BUY" else entry_price + sl_distance
            logging.debug(f"SL {self.symbol} (ATR_MULTIPLE): {sl_price:.{self.digits}f}")

        # 2. Calcul TP
        tp_calculated_structurally = False
        if self.tp_strategy == 'SMC_LIQUIDITY_TARGET' and trade_signal.get('tp_price', 0.0) > 0:
            target_price = trade_signal['tp_price']
            if (direction == "BUY" and target_price > entry_price) or (direction == "SELL" and target_price < entry_price):
                tp_buffer_abs = float(self.tp_buffer_pips) * self.point
                potential_tp = target_price - tp_buffer_abs if direction == "BUY" else target_price + tp_buffer_abs
                # Re-valider après buffer
                if (direction == "BUY" and potential_tp > entry_price) or (direction == "SELL" and potential_tp < entry_price):
                     tp_price = potential_tp; tp_calculated_structurally = True
                     logging.debug(f"TP {self.symbol} (SMC_LIQUIDITY_TARGET): {tp_price:.{self.digits}f}")
                else: logging.warning(f"TP SMC {self.symbol} ({potential_tp}) invalide après buffer. Fallback ATR.")
            else: logging.warning(f"TP SMC {self.symbol} cible ({target_price}) non profitable vs entrée ({entry_price}). Fallback ATR.")
        
        if not tp_calculated_structurally:
            if atr == 0.0: logging.error("ATR invalide, impossible de calculer TP fallback."); return 0.0, 0.0
            tp_distance = float(self.tp_atr_multiplier) * atr
            tp_price = entry_price + tp_distance if direction == "BUY" else entry_price - tp_distance
            logging.debug(f"TP {self.symbol} (ATR_MULTIPLE): {tp_price:.{self.digits}f}")

        # 3. Validation finale et Arrondi
        if (direction == "BUY" and (sl_price >= entry_price or tp_price <= entry_price)) or \
           (direction == "SELL" and (sl_price <= entry_price or tp_price >= entry_price)):
             logging.error(f"Erreur logique SL/TP {self.symbol}: E={entry_price}, SL={sl_price}, TP={tp_price}. Annulation.")
             return 0.0, 0.0
             
        sl_price = round(sl_price, self.digits)
        tp_price = round(tp_price, self.digits)
        return sl_price, tp_price


    def manage_open_positions(self, positions: list, tick, ohlc_data: pd.DataFrame):
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        self.ohlc_data_cache = ohlc_data
        if not positions: return
        if self.ptp_rules:
            for rule in self.ptp_rules:
                rr_target = Decimal(str(rule.get('rr', 1.0)))
                percentage_to_close = Decimal(str(rule.get('percentage', 50.0))) / Decimal('100.0')
                self._apply_ptp(positions, tick, rr_target, percentage_to_close)
        if self.breakeven_rules.get('enabled', False):
            if self.breakeven_rules.get('move_to_be_plus_on_ptp1', False) and self.ptp_rules:
                ptp1_rr = Decimal(str(self.ptp_rules[0].get('rr', 1.0)))
                self._apply_breakeven_on_ptp(positions, tick, ptp1_rr)
            else:
                trigger_pips = self.breakeven_rules.get('trigger_pips', 0)
                if trigger_pips > 0: self._apply_breakeven_pips(positions, tick, trigger_pips)
        if self.trailing_stop_rules.get('enabled', False):
            activation_multiple = Decimal(str(self.trailing_stop_rules.get('activation_multiple', 2.0)))
            trailing_multiple = Decimal(str(self.trailing_stop_rules.get('trailing_multiple', 1.5)))
            atr = Decimal(str(self._calculate_atr(ohlc_data, 14)))
            if atr > 0: self._apply_trailing_stop_atr(positions, tick, atr, activation_multiple, trailing_multiple)

    def _apply_ptp(self, positions: list, tick, rr_target: Decimal, percentage_to_close: Decimal):
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        for pos in positions:
            if f"PTP{rr_target}" in pos.comment: continue
            initial_sl = pos.sl; initial_entry = pos.price_open
            if pos.type == mt5.ORDER_TYPE_BUY:
                sl_distance = Decimal(str(initial_entry - initial_sl))
                if sl_distance <= 0: continue
                tp_target = Decimal(str(initial_entry)) + (sl_distance * rr_target)
                current_price = Decimal(str(tick.bid))
                if current_price >= tp_target: self._execute_partial_close(pos, percentage_to_close, f"PTP{rr_target}")
            elif pos.type == mt5.ORDER_TYPE_SELL:
                sl_distance = Decimal(str(initial_sl - initial_entry))
                if sl_distance <= 0: continue
                tp_target = Decimal(str(initial_entry)) - (sl_distance * rr_target)
                current_price = Decimal(str(tick.ask))
                if current_price <= tp_target: self._execute_partial_close(pos, percentage_to_close, f"PTP{rr_target}")

    def _execute_partial_close(self, position, percentage: Decimal, new_comment_flag: str):
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        try:
            volume_to_close = Decimal(str(position.volume)) * percentage
            step = Decimal(str(self.volume_step))
            volume_to_close = (volume_to_close // step) * step
            volume_remaining = Decimal(str(position.volume)) - volume_to_close
            vol_min_d = Decimal(str(self.volume_min))
            if volume_to_close < vol_min_d or volume_remaining < vol_min_d:
                 if volume_to_close < vol_min_d: logging.info(f"PTP {position.ticket}: Annulé (volume < min).")
                 return
            logging.info(f"PTP {new_comment_flag} atteint {self.symbol} ({position.ticket}). Fermeture {volume_to_close} lots.")
            new_comment = (position.comment or "") + f"|{new_comment_flag}"
            self.executor.close_partial_position(position, float(volume_to_close), new_comment)
        except Exception as e: logging.error(f"Erreur PTP (Ticket {position.ticket}): {e}", exc_info=True)

    def _apply_breakeven_pips(self, positions: list, tick, trigger_pips: int):
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        pips_plus = self.breakeven_rules.get('pips_plus', 1.0)
        for pos in positions:
            if pos.sl == pos.price_open: continue
            if "BE_APPLIED" in pos.comment: continue
            trigger_distance_points = Decimal(str(trigger_pips)) * Decimal(str(self.point))
            sl_new_distance_points = Decimal(str(pips_plus)) * Decimal(str(self.point))
            if pos.type == mt5.ORDER_TYPE_BUY:
                current_profit_points = Decimal(str(tick.bid)) - Decimal(str(pos.price_open))
                if current_profit_points >= trigger_distance_points:
                    new_sl = round(float(Decimal(str(pos.price_open)) + sl_new_distance_points), self.digits)
                    if new_sl > pos.sl:
                         logging.info(f"Breakeven (Pips) {self.symbol} (Ticket: {pos.ticket}). SL -> {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_profit_points = Decimal(str(pos.price_open)) - Decimal(str(tick.ask))
                if current_profit_points >= trigger_distance_points:
                    new_sl = round(float(Decimal(str(pos.price_open)) - sl_new_distance_points), self.digits)
                    if pos.sl == 0 or new_sl < pos.sl:
                         logging.info(f"Breakeven (Pips) {self.symbol} (Ticket: {pos.ticket}). SL -> {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")

    def _apply_breakeven_on_ptp(self, positions: list, tick, ptp1_rr: Decimal):
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        pips_plus = self.breakeven_rules.get('pips_plus_on_ptp1', 5.0)
        for pos in positions:
            if f"PTP{ptp1_rr}" in pos.comment and "BE_APPLIED" not in pos.comment:
                sl_new_distance_points = Decimal(str(pips_plus)) * Decimal(str(self.point))
                if pos.type == mt5.ORDER_TYPE_BUY:
                    new_sl = round(float(Decimal(str(pos.price_open)) + sl_new_distance_points), self.digits)
                    if new_sl > pos.sl:
                         logging.info(f"Breakeven (Post-PTP1) {self.symbol} ({pos.ticket}). SL -> {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")
                elif pos.type == mt5.ORDER_TYPE_SELL:
                    new_sl = round(float(Decimal(str(pos.price_open)) - sl_new_distance_points), self.digits)
                    if pos.sl == 0 or new_sl < pos.sl:
                         logging.info(f"Breakeven (Post-PTP1) {self.symbol} ({pos.ticket}). SL -> {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "BE_APPLIED")

    def _apply_trailing_stop_atr(self, positions: list, tick, atr: Decimal, activation_multiple: Decimal, trailing_multiple: Decimal):
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        if atr <= 0: return
        activation_distance_points_std = activation_multiple * atr / Decimal(str(self.point))
        trailing_distance_points = trailing_multiple * atr
        
        for pos in positions:
            current_sl_price = Decimal(str(pos.sl))
            entry_price = Decimal(str(pos.price_open))
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                current_price = Decimal(str(tick.bid))
                current_profit_points = (current_price - entry_price) / Decimal(str(self.point))
                
                if current_profit_points >= activation_distance_points_std:
                    potential_new_sl_d = current_price - trailing_distance_points
                    if potential_new_sl_d > current_sl_price and potential_new_sl_d > entry_price:
                         new_sl = round(float(potential_new_sl_d), self.digits)
                         logging.debug(f"Trailing Stop (BUY) {self.symbol} ({pos.ticket}). SL -> {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "TS_APPLIED")
            
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_price = Decimal(str(tick.ask))
                current_profit_points = (entry_price - current_price) / Decimal(str(self.point))

                if current_profit_points >= activation_distance_points_std:
                    potential_new_sl_d = current_price + trailing_distance_points
                    new_sl = round(float(potential_new_sl_d), self.digits)
                    if (current_sl_price == 0 or potential_new_sl_d < current_sl_price) and potential_new_sl_d < entry_price:
                         logging.debug(f"Trailing Stop (SELL) {self.symbol} ({pos.ticket}). SL -> {new_sl}")
                         self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "TS_APPLIED")

    def check_max_concurrent_risk(self, equity: float) -> bool:
        # ... (inchangé) ...
        # (code identique à la version précédente v18.1.6)
        if self.max_concurrent_risk_pct <= 0: return True
        try:
            magic_number = self.config['trading_settings'].get('magic_number', 0)
            open_positions = self.executor.get_open_positions(magic_number)
            current_total_risk_pct = Decimal('0.0')
            for pos in open_positions:
                 is_at_be_or_profit = False
                 if (pos.type == mt5.ORDER_TYPE_BUY and pos.sl > pos.price_open) or \
                    (pos.type == mt5.ORDER_TYPE_SELL and pos.sl < pos.price_open and pos.sl != 0):
                     is_at_be_or_profit = True
                 if not is_at_be_or_profit:
                     current_total_risk_pct += self.risk_per_trade_pct
            potential_total_risk = current_total_risk_pct + self.risk_per_trade_pct
            limit_pct = self.max_concurrent_risk_pct
            if potential_total_risk > limit_pct:
                 logging.warning(f"Check Risque Concurrent: {potential_total_risk*100:.1f}% > Limite {limit_pct*100:.1f}%.")
                 return False
            logging.info(f"Check Risque Concurrent: {potential_total_risk*100:.1f}% <= Limite {limit_pct*100:.1f}%.")
            return True
        except Exception as e:
             logging.error(f"Erreur vérification risque concurrent: {e}", exc_info=True)
             return False
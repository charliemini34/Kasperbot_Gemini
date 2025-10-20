# Fichier: src/risk/risk_manager.py
# Version: 18.1.3 (Executor-Attr-Fix)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, math, decimal, src.constants
# Description: Corrige AttributeError pour l'accès à l'objet MT5 via executor.

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from decimal import Decimal, ROUND_DOWN # Pour un arrondi précis au step
from datetime import datetime
import pytz
from typing import Tuple, Optional, Dict, List

from src.constants import BUY, SELL
# --- NOUVEAU : Importer MT5Executor pour type hinting ---
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.execution.mt5_executor import MT5Executor
# --- FIN NOUVEAU ---

class RiskManager:
    """
    Gère le risque avec SL/TP configurables et PTP.
    v18.1.3: Correction accès attribut connexion MT5 via executor.get_mt5_connection().
    """
    # --- MODIFICATION : Type hint pour executor ---
    def __init__(self, config: dict, executor: 'MT5Executor', symbol: str):
    # --- FIN MODIFICATION ---
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config
        self._executor = executor # Garde l'instance de l'executor
        self._symbol: str = symbol

        # --- CORRECTION : Utiliser la nouvelle méthode get_mt5_connection ---
        self.mt5_conn = self._executor.get_mt5_connection()
        if self.mt5_conn is None:
            self.log.critical("Impossible d'obtenir l'objet de connexion MT5 depuis l'executor.")
            raise ValueError("Connexion MT5 non disponible via executor.")

        self.symbol_info = self.mt5_conn.symbol_info(self._symbol)
        self.account_info = self.mt5_conn.account_info()
        # --- FIN CORRECTION ---


        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les infos symbole/compte pour {self._symbol}.")
            raise ValueError("Infos MT5 manquantes.")

        self.point: float = self.symbol_info.point
        self.digits: int = self.symbol_info.digits
        self.risk_settings = self._config.get('risk_management', {})
        self.sl_strategy = self.risk_settings.get('sl_strategy', 'ATR_MULTIPLE')
        self.sl_buffer_pips = self.risk_settings.get('sl_buffer_pips', 5)
        self.partial_tp_config = self.risk_settings.get('partial_tp', {})
        self.trade_calc_mode = self.symbol_info.trade_calc_mode
        self.trade_contract_size = Decimal(str(self.symbol_info.trade_contract_size))
        self.currency_profit = self.symbol_info.currency_profit
        self.currency_margin = self.symbol_info.currency_margin
        self.account_currency = self.account_info.currency


    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        # Utilise self.mt5_conn (corrigé précédemment)
        loss_limit_percent = self.risk_settings.get('daily_loss_limit_percent', 2.0)
        if loss_limit_percent <= 0: return False, 0.0
        try:
            today_start_utc = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            history_deals = self.mt5_conn.history_deals_get(today_start_utc, datetime.utcnow())
            if history_deals is None: return False, 0.0
            magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == magic_number and deal.entry == 1)
            loss_limit_amount = (Decimal(str(self.account_info.equity)) * Decimal(str(loss_limit_percent))) / Decimal('100.0')
            if daily_pnl < 0 and abs(Decimal(str(daily_pnl))) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) atteint limite {loss_limit_percent}%.")
                return True, daily_pnl
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur calcul limite perte jour : {e}", exc_info=True)
            return False, 0.0

    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        # Utilise _calculate_volume qui utilise maintenant self.mt5_conn via get_conversion_rate_decimal etc.
        # Donc pas besoin de changer ici
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal invalide: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0
            risk_percent = self.risk_settings.get('risk_per_trade', 0.01)
            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)
            if ideal_sl <= 0 or ideal_tp <= 0 or abs(price - ideal_sl) < self.symbol_info.point * 5 or abs(price - ideal_tp) < self.symbol_info.point * 5 :
                self.log.error(f"SL/TP invalide ou trop proche. SL: {ideal_sl}, TP: {ideal_tp}, Prix: {price}. Trade annulé.")
                return 0.0, 0.0, 0.0
            final_volume = self._calculate_volume(Decimal(str(equity)), Decimal(str(risk_percent)), Decimal(str(price)), Decimal(str(ideal_sl)))
            if final_volume is None: # Si _calculate_volume a échoué
                 self.log.error("Échec du calcul de volume.")
                 return 0.0, 0.0, 0.0
            final_volume_float = float(final_volume) # Convertir en float pour le reste
            vol_min_from_api = self.symbol_info.volume_min
            vol_step_from_api = self.symbol_info.volume_step
            self.log.debug(f"DEBUG VOLUME pour {self._symbol}: Vol Final={final_volume_float:.4f}, Vol Min API={vol_min_from_api}, Vol Step API={vol_step_from_api}")
            if final_volume_float < vol_min_from_api and final_volume_float > 0:
                 self.log.warning(f"Volume final ({final_volume_float:.4f}) < Min API ({vol_min_from_api}). Trade annulé.")
                 return 0.0, 0.0, 0.0
            elif final_volume_float <= 0:
                 self.log.warning(f"Volume final calculé <= 0 pour {self._symbol}. Trade annulé.")
                 return 0.0, 0.0, 0.0
            return final_volume_float, ideal_sl, ideal_tp
        except Exception as e:
            self.log.error(f"Erreur calcul paramètres trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0

    def _calculate_pip_value_in_account_currency(self, volume_lots: Decimal) -> Optional[Decimal]:
        # Utilise self.mt5_conn via get_conversion_rate_decimal
        # Donc pas besoin de changer ici
        try:
            point_value_profit_curr = Decimal(str(self.point)) * self.trade_contract_size * volume_lots
            calc_mode = self.trade_calc_mode
            tick_value = Decimal(str(self.symbol_info.tick_value))
            tick_size = Decimal(str(self.symbol_info.tick_size))

            if calc_mode == mt5.SYMBOL_CALC_MODE_FOREX or calc_mode == mt5.SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE:
                if self.currency_profit == self.account_currency:
                    point_value_account_curr = point_value_profit_curr
                else:
                    conversion_rate = self.get_conversion_rate_decimal(self.currency_profit, self.account_currency)
                    if conversion_rate is None: return None
                    point_value_account_curr = point_value_profit_curr * conversion_rate
            elif calc_mode == mt5.SYMBOL_CALC_MODE_CFD or \
                 calc_mode == mt5.SYMBOL_CALC_MODE_CFDINDEX or \
                 calc_mode == mt5.SYMBOL_CALC_MODE_CFDLEVERAGE:
                 if tick_size == 0: self.log.error("Tick size est zéro."); return None
                 point_value_account_curr = (tick_value / tick_size) * Decimal(str(self.point)) * volume_lots
                 if self.currency_margin != self.account_currency:
                      conversion_rate = self.get_conversion_rate_decimal(self.currency_margin, self.account_currency)
                      if conversion_rate is None: return None
                      point_value_account_curr = point_value_account_curr * conversion_rate
            elif calc_mode == mt5.SYMBOL_CALC_MODE_FUTURES:
                 if tick_size == 0: self.log.error("Tick size est zéro."); return None
                 point_value_account_curr = (tick_value / tick_size) * Decimal(str(self.point)) * volume_lots
                 if self.currency_margin != self.account_currency:
                      conversion_rate = self.get_conversion_rate_decimal(self.currency_margin, self.account_currency)
                      if conversion_rate is None: return None
                      point_value_account_curr = point_value_account_curr * conversion_rate
            else:
                self.log.warning(f"Mode de calcul {calc_mode} non géré explicitement pour {self._symbol}. Utilisation fallback simple.")
                point_value_account_curr = point_value_profit_curr
                if self.currency_profit != self.account_currency:
                    conversion_rate = self.get_conversion_rate_decimal(self.currency_profit, self.account_currency)
                    if conversion_rate is None: return None
                    point_value_account_curr = point_value_profit_curr * conversion_rate
            return point_value_account_curr
        except Exception as e:
            self.log.error(f"Erreur calcul valeur pip: {e}", exc_info=True)
            return None

    def _calculate_volume(self, equity: Decimal, risk_percent: Decimal, entry_price: Decimal, sl_price: Decimal) -> Optional[Decimal]:
        # Utilise self.mt5_conn via get_conversion_rate_decimal etc.
        # Donc pas besoin de changer ici
        self.log.debug("--- DÉBUT CALCUL VOLUME ---")
        try:
            risk_amount = equity * risk_percent
            self.log.debug(f"1. Capital: {equity:.2f} | Risque: {risk_percent:.2%} -> Montant Risqué: {risk_amount:.2f} {self.account_currency}")
            sl_distance_points = (abs(entry_price - sl_price) / Decimal(str(self.point))).to_integral_value(rounding=ROUND_DOWN) + 1
            if sl_distance_points <= 0: self.log.error("Distance SL nulle ou négative."); return Decimal('0.0')
            self.log.debug(f"2. Distance SL (Points): {sl_distance_points}")
            point_value_1_lot = self._calculate_pip_value_in_account_currency(Decimal('1.0'))
            if point_value_1_lot is None or point_value_1_lot <= 0: self.log.error("Impossible calculer valeur point."); return Decimal('0.0')
            self.log.debug(f"3. Valeur 1 Point/1 Lot ({self.account_currency}): {point_value_1_lot:.4f}")
            loss_for_1_lot = sl_distance_points * point_value_1_lot
            if loss_for_1_lot <= 0: self.log.error("Perte calculée pour 1 lot <= 0."); return Decimal('0.0')
            self.log.debug(f"4. Perte/1 Lot ({self.account_currency}): {loss_for_1_lot:.2f}")
            raw_volume = risk_amount / loss_for_1_lot
            self.log.debug(f"5. Volume Brut: {raw_volume:.8f} lots")
            volume_step = Decimal(str(self.symbol_info.volume_step))
            if volume_step <= 0: self.log.warning(f"Volume step invalide ({volume_step})."); adjusted_volume = raw_volume
            else:
                num_steps = (raw_volume / volume_step).to_integral_value(rounding=ROUND_DOWN)
                adjusted_volume = num_steps * volume_step
                self.log.debug(f"6. Volume Ajusté Step ({volume_step}): {adjusted_volume:.8f}")
            vol_max = Decimal(str(self.symbol_info.volume_max))
            final_volume = max(Decimal('0'), min(vol_max, adjusted_volume))
            self.log.debug(f"7. Volume Final (après min/max/step): {final_volume:.4f} (Min API: {self.symbol_info.volume_min}, Max API: {vol_max})")
            vol_min = Decimal(str(self.symbol_info.volume_min))
            if final_volume < vol_min and final_volume > 0:
                 self.log.warning(f"Volume final ({final_volume:.4f}) < Min API ({vol_min}). Vol mis à 0.")
                 return Decimal('0.0')
            elif final_volume <= 0: return Decimal('0.0')
            else: return final_volume
        except Exception as e:
            self.log.error(f"Erreur inattendue dans _calculate_volume: {e}", exc_info=True)
            self.log.error(f"Inputs: equity={equity}, risk%={risk_percent}, entry={entry_price}, sl={sl_price}")
            return None

    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        # Utilise _calculate_volume qui est corrigé
        # Donc pas besoin de changer ici
        # (code identique à la version précédente 18.1.2)
        sl = 0.0; tp = 0.0; direction = trade_signal['direction']
        tp_strategy = self.risk_settings.get('tp_strategy', 'ATR_MULTIPLE')
        atr = self.calculate_atr(ohlc_data, self.risk_settings.get('atr_settings', {}).get('default', {}).get('period', 14))
        sl_calculated_structurally = False
        if self.sl_strategy == "SMC_STRUCTURE":
            sl_structure_price = trade_signal.get('sl_structure_price')
            if sl_structure_price and atr and atr > 0:
                sl_buffer = self.sl_buffer_pips * self.point
                potential_sl = sl_structure_price - sl_buffer if direction == BUY else sl_structure_price + sl_buffer
                min_sl_dist = atr * 0.5
                if abs(price - potential_sl) >= min_sl_dist:
                    temp_volume_d = self._calculate_volume(Decimal(str(self.account_info.equity)), Decimal(str(self.risk_settings.get('risk_per_trade', 0.01))), Decimal(str(price)), Decimal(str(potential_sl)))
                    if temp_volume_d is not None and temp_volume_d > 0 and temp_volume_d >= Decimal(str(self.symbol_info.volume_min)):
                        sl = potential_sl; sl_calculated_structurally = True
                        self.log.debug(f"Stratégie SL Structurelle: SL={sl:.{self.digits}f}")
                    else: self.log.warning(f"SL Structurel ({potential_sl:.{self.digits}f}) -> volume ({float(temp_volume_d or 0):.4f}) <= min ({self.symbol_info.volume_min}). Fallback ATR.")
                else: self.log.warning(f"SL Structurel ({potential_sl:.{self.digits}f}) trop proche du prix ({price:.{self.digits}f}). Fallback ATR.")
            else: self.log.warning("SL Structurel invalide/manquant ou ATR invalide. Fallback ATR.")
        if not sl_calculated_structurally:
            if atr is None or atr <= 0: return 0.0, 0.0
            sl_multiple = self.risk_settings.get('atr_settings', {}).get('default', {}).get('sl_multiple', 1.5)
            sl = price - (atr * sl_multiple) if direction == BUY else price + (atr * sl_multiple)
            self.log.debug(f"Stratégie SL ATR utilisée: SL={sl:.{self.digits}f}")
        tp_calculated = False
        if tp_strategy == "SMC_LIQUIDITY_TARGET":
            target_price = trade_signal.get('target_price')
            if target_price:
                is_valid = (direction == BUY and target_price > price) or (direction == SELL and target_price < price)
                if is_valid:
                    tp_offset = self.point * 10
                    tp = target_price - tp_offset if direction == BUY else target_price + tp_offset
                    if (direction == BUY and tp > price) or (direction == SELL and tp < price):
                         self.log.debug(f"Stratégie TP SMC: Cible {target_price:.{self.digits}f} -> TP={tp:.{self.digits}f}")
                         tp_calculated = True
                    else: self.log.warning(f"TP SMC: Cible {target_price:.{self.digits}f} mais offset a rendu TP ({tp:.{self.digits}f}) invalide vs Prix {price:.{self.digits}f}. Fallback ATR.")
                else: self.log.warning(f"TP SMC: Cible {target_price:.{self.digits}f} invalide vs Prix {price:.{self.digits}f}. Fallback ATR.")
            else: self.log.warning("TP SMC choisi mais cible manquante. Fallback ATR.")
        if not tp_calculated:
            if atr is None or atr <= 0: return sl, 0.0
            tp_multiple = self.risk_settings.get('atr_settings', {}).get('default', {}).get('tp_multiple', 3.0)
            tp = price + (atr * tp_multiple) if direction == BUY else price - (atr * tp_multiple)
            if abs(price - tp) < self.point * 5:
                 self.log.error(f"TP ATR ({tp:.{self.digits}f}) trop proche du prix ({price:.{self.digits}f}). TP invalide.")
                 return sl, 0.0
            self.log.debug(f"Stratégie TP ATR utilisée: TP={tp:.{self.digits}f}")
        if sl > 0 and tp > 0 and abs(tp - price) < abs(sl - price):
            self.log.warning(f"TP final ({tp:.{self.digits}f}) plus proche que SL final ({sl:.{self.digits}f}) (Ratio < 1).")
        if sl <= 0 or tp <= 0:
             self.log.error(f"Calcul final SL/TP invalide. SL={sl}, TP={tp}")
             return 0.0, 0.0
        return round(sl, self.digits), round(tp, self.digits)

    def get_conversion_rate_decimal(self, from_currency: str, to_currency: str) -> Optional[Decimal]:
        # Utilise self.mt5_conn
        if from_currency == to_currency: return Decimal('1.0')
        pair1 = f"{from_currency}{to_currency}"; info1 = self.mt5_conn.symbol_info_tick(pair1)
        if info1 and info1.ask > 0: rate = Decimal(str(info1.ask)); return rate
        pair2 = f"{to_currency}{from_currency}"; info2 = self.mt5_conn.symbol_info_tick(pair2)
        if info2 and info2.bid > 0:
            try: rate = Decimal('1.0') / Decimal(str(info2.bid)); return rate
            except Exception: return None
        for pivot in ["USD", "EUR", "GBP"]:
             if from_currency != pivot and to_currency != pivot:
                 rate1 = self.get_conversion_rate_decimal(from_currency, pivot); rate2 = self.get_conversion_rate_decimal(pivot, to_currency)
                 if rate1 is not None and rate2 is not None: return rate1 * rate2
        self.log.error(f"Impossible trouver conversion {from_currency} -> {to_currency}"); return None

    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        # ... (inchangé) ...
        # (code identique à la version précédente 18.1.2)
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period: return None
        required_cols = ['high', 'low', 'close']
        if not all(col in ohlc_data.columns for col in required_cols): return None
        df_copy = ohlc_data.copy(); df_copy[required_cols] = df_copy[required_cols].apply(pd.to_numeric, errors='coerce')
        df_copy.dropna(subset=required_cols, inplace=True);
        if len(df_copy) < period: return None
        high_low = df_copy['high'] - df_copy['low']; high_close = np.abs(df_copy['high'] - df_copy['close'].shift()); low_close = np.abs(df_copy['low'] - df_copy['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1); true_range = np.max(ranges, axis=1)
        atr_series = true_range.ewm(span=period, adjust=False).mean(); last_atr = atr_series.iloc[-1]
        if pd.isna(last_atr): return None
        return last_atr

    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        # Utilise self.mt5_conn via _apply_partial_take_profit
        # Donc pas besoin de changer ici
        # (code identique à la version précédente 18.1.2)
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return
        if self.partial_tp_config.get('enabled', False):
            self._apply_partial_take_profit(positions, current_tick)
        if self.risk_settings.get('breakeven', {}).get('enabled', False):
            self._apply_breakeven(positions, current_tick, self.risk_settings.get('breakeven', {}))
        if self.risk_settings.get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, self.risk_settings)

    def _apply_partial_take_profit(self, positions: list, tick):
        # Utilise self.mt5_conn
        # (code identique à la version précédente 18.1.2)
        levels = self.partial_tp_config.get('levels', [])
        if not levels: return
        for pos in positions:
            context = self._executor._trade_context.get(pos.ticket)
            if not context or pos.sl <= 0: continue
            current_pos_info = self.mt5_conn.positions_get(ticket=pos.ticket)
            if not current_pos_info: continue
            current_pos = current_pos_info[0]
            initial_volume = context.get('initial_volume', 0)
            remaining_volume = context.get('remaining_volume', current_pos.volume)
            partial_tp_state = context.get('partial_tp_state', [])
            if initial_volume <= 0 or len(partial_tp_state) != len(levels): continue
            risk_distance = abs(pos.price_open - pos.sl)
            if risk_distance < self.point * 2: continue
            pnl_price = (tick.bid - pos.price_open) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - tick.ask)
            first_ptp_hit_this_cycle = False
            for i, level in enumerate(levels):
                if not partial_tp_state[i]:
                    target_distance = risk_distance * level.get('rr', 0)
                    percentage_to_close = level.get('percentage', 0)
                    if target_distance > 0 and percentage_to_close > 0 and pnl_price >= target_distance:
                        self.log.info(f"PTP #{i+1} (RR {level['rr']}) atteint pour #{pos.ticket}.")
                        absolute_volume_to_close = initial_volume * (percentage_to_close / 100.0)
                        symbol_info = self.mt5_conn.symbol_info(pos.symbol)
                        if not symbol_info: continue
                        vol_step = symbol_info.volume_step; vol_min = symbol_info.volume_min; vol_digits = int(abs(Decimal(str(vol_step)).log10())) if vol_step > 0 else 2
                        if vol_step > 0:
                            vol_to_close_d = Decimal(str(absolute_volume_to_close)); vol_step_d = Decimal(str(vol_step))
                            num_steps = (vol_to_close_d / vol_step_d).to_integral_value(rounding=ROUND_DOWN)
                            volume_to_close_adjusted = float(num_steps * vol_step_d)
                        else: volume_to_close_adjusted = absolute_volume_to_close
                        volume_to_close_final = round(min(volume_to_close_adjusted, remaining_volume), vol_digits)
                        if volume_to_close_final < vol_min and volume_to_close_final > 0: self.log.warning(f"Vol PTP #{i+1} ({volume_to_close_final:.{vol_digits}f}) < min ({vol_min}). Ignoré."); continue
                        self.log.debug(f"PTP #{i+1} #{pos.ticket}: Init={initial_volume}, Restant={remaining_volume}, %={percentage_to_close}, Abs={absolute_volume_to_close:.6f}, Adj={volume_to_close_adjusted:.6f}, Final={volume_to_close_final:.{vol_digits}f}")
                        if volume_to_close_final > 0:
                            if self._executor.close_partial_position(current_pos, volume_to_close_final):
                                remaining_volume_d = Decimal(str(remaining_volume)) - Decimal(str(volume_to_close_final))
                                context['remaining_volume'] = float(remaining_volume_d)
                                context['partial_tp_state'][i] = True
                                self.log.info(f"PTP #{i+1} exécuté #{pos.ticket}. Restant: {context['remaining_volume']:.{vol_digits}f}")
                                if i == 0 and self.partial_tp_config.get('move_sl_to_be_after_tp1', False): first_ptp_hit_this_cycle = True
                            else: self.log.error(f"Échec exécution PTP #{i+1} #{pos.ticket}."); break
                        else: self.log.warning(f"Volume final PTP #{i+1} pour #{pos.ticket} est 0. Ignoré.")
            if first_ptp_hit_this_cycle:
                 pips_plus = self.partial_tp_config.get('be_pips_plus_after_tp1', 5)
                 be_sl_price = (pos.price_open + (pips_plus * self.point)) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - (pips_plus * self.point))
                 if (pos.type == mt5.ORDER_TYPE_BUY and be_sl_price > pos.sl) or \
                    (pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or be_sl_price < pos.sl)):
                      self.log.info(f"BE+{pips_plus} pips après PTP1 #{pos.ticket}. New SL: {be_sl_price:.{self.digits}f}")
                      self._executor.modify_position(pos.ticket, be_sl_price, pos.tp)

    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        # ... (inchangé) ...
        # (code identique à la version précédente 18.1.2)
        trigger_pips = be_cfg.get('trigger_pips', 150); pips_plus = be_cfg.get('pips_plus', 10)
        for pos in positions:
            context = self._executor._trade_context.get(pos.ticket)
            be_pips_plus_ptp1 = self.partial_tp_config.get('be_pips_plus_after_tp1', 5)
            be_sl_price_ptp1 = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY: be_sl_price_ptp1 = pos.price_open + (be_pips_plus_ptp1 * self.point)
            elif pos.type == mt5.ORDER_TYPE_SELL: be_sl_price_ptp1 = pos.price_open - (be_pips_plus_ptp1 * self.point)
            if pos.sl != 0 and abs(pos.sl - be_sl_price_ptp1) < self.point: continue
            pnl_pips = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_pips = (tick.bid - pos.price_open) / self.point
                if pos.sl != 0 and pos.sl < pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open + (pips_plus * self.point)
                    if breakeven_sl > pos.sl:
                         self.log.info(f"BE déclenché #{pos.ticket}. New SL: {breakeven_sl:.{self.digits}f}")
                         self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_pips = (pos.price_open - tick.ask) / self.point
                if pos.sl != 0 and pos.sl > pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open - (pips_plus * self.point)
                    if pos.sl == 0 or breakeven_sl < pos.sl:
                         self.log.info(f"BE déclenché #{pos.ticket}. New SL: {breakeven_sl:.{self.digits}f}")
                         self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)

    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        # ... (inchangé) ...
        # (code identique à la version précédente 18.1.2)
        ts_cfg = risk_cfg.get('trailing_stop_atr', {}); atr_cfg = risk_cfg.get('atr_settings', {}).get('default', {})
        period = atr_cfg.get('period', 14); atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0: return
        activation_multiple = ts_cfg.get('activation_multiple', 2.0); trailing_multiple = ts_cfg.get('trailing_multiple', 1.8)
        for pos in positions:
            new_sl = pos.sl; current_sl = pos.sl
            if pos.type == mt5.ORDER_TYPE_BUY:
                if (tick.bid - pos.price_open) >= (atr * activation_multiple):
                    potential_new_sl = tick.bid - (atr * trailing_multiple)
                    if potential_new_sl > current_sl and potential_new_sl > pos.price_open: new_sl = potential_new_sl
            elif pos.type == mt5.ORDER_TYPE_SELL:
                if (pos.price_open - tick.ask) >= (atr * activation_multiple):
                    potential_new_sl = tick.ask + (atr * trailing_multiple)
                    if (current_sl == 0 or potential_new_sl < current_sl) and potential_new_sl < pos.price_open: new_sl = potential_new_sl
            new_sl_rounded = round(new_sl, self.digits)
            if new_sl_rounded != round(current_sl, self.digits):
                self.log.info(f"TSL: Update SL #{pos.ticket} -> {new_sl_rounded:.{self.digits}f}")
                self._executor.modify_position(pos.ticket, new_sl_rounded, pos.tp)
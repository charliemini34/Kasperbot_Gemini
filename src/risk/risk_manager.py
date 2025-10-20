# Fichier: src/risk/risk_manager.py
# Version: 18.1.0 (Partial-TP-Logic)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, math, src.constants
# Description: Implémente la logique de gestion des Take Profits Partiels (PTP).

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from typing import Tuple, Optional, Dict, List

from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque avec SL/TP configurables et Take Profits Partiels (PTP).
    v18.1.0: Ajout de la logique PTP _apply_partial_take_profit.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config
        self._executor = executor
        self._symbol: str = symbol

        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()

        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les infos symbole/compte pour {self._symbol}.")
            raise ValueError("Infos MT5 manquantes.")

        self.point: float = self.symbol_info.point
        self.digits: int = self.symbol_info.digits
        self.risk_settings = self._config.get('risk_management', {})
        self.sl_strategy = self.risk_settings.get('sl_strategy', 'ATR_MULTIPLE')
        self.sl_buffer_pips = self.risk_settings.get('sl_buffer_pips', 5)
        # --- NOUVEAU : Récupérer config PTP ---
        self.partial_tp_config = self.risk_settings.get('partial_tp', {})

    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        # ... (inchangé) ...
        loss_limit_percent = self.risk_settings.get('daily_loss_limit_percent', 2.0)
        if loss_limit_percent <= 0: return False, 0.0
        try:
            today_start_utc = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            history_deals = self._executor._mt5.history_deals_get(today_start_utc, datetime.utcnow())
            if history_deals is None: return False, 0.0
            magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == magic_number and deal.entry == 1)
            loss_limit_amount = (self.account_info.equity * loss_limit_percent) / 100.0
            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) atteint limite {loss_limit_percent}%.")
                return True, daily_pnl
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur calcul limite perte jour : {e}", exc_info=True)
            return False, 0.0

    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        # ... (inchangé) ...
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal invalide: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0
            risk_percent = self.risk_settings.get('risk_per_trade', 0.01)
            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)
            if ideal_sl <= 0 or ideal_tp <= 0 or abs(price - ideal_sl) < self.symbol_info.point * 5 or abs(price - ideal_tp) < self.symbol_info.point * 5 :
                self.log.error(f"SL/TP invalide ou trop proche. SL: {ideal_sl}, TP: {ideal_tp}, Prix: {price}. Trade annulé.")
                return 0.0, 0.0, 0.0
            final_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)
            vol_min_from_api = self.symbol_info.volume_min
            vol_step_from_api = self.symbol_info.volume_step
            self.log.debug(f"DEBUG VOLUME pour {self._symbol}: Vol Final={final_volume:.4f}, Vol Min API={vol_min_from_api}, Vol Step API={vol_step_from_api}")
            if final_volume < vol_min_from_api and final_volume > 0:
                 self.log.warning(f"Volume final ({final_volume:.4f}) < Min API ({vol_min_from_api}). Trade annulé.")
                 return 0.0, 0.0, 0.0
            elif final_volume <= 0:
                 self.log.warning(f"Volume final calculé <= 0 pour {self._symbol}. Trade annulé.")
                 return 0.0, 0.0, 0.0
            return final_volume, ideal_sl, ideal_tp
        except Exception as e:
            self.log.error(f"Erreur calcul paramètres trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0

    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        # ... (inchangé) ...
        self.log.debug("--- DÉBUT CALCUL VOLUME ---")
        risk_amount = equity * risk_percent
        sl_distance = abs(entry_price - sl_price)
        if sl_distance < self.point * 2: self.log.error("Distance SL trop faible."); return 0.0
        loss_per_lot_profit = sl_distance * self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        loss_per_lot_account = loss_per_lot_profit
        if profit_currency != self.account_info.currency:
            rate = self.get_conversion_rate(profit_currency, self.account_info.currency)
            if not rate or rate <= 0: return 0.0
            loss_per_lot_account *= rate
        if loss_per_lot_account <= 0: self.log.error("Perte/lot nulle ou négative."); return 0.0
        raw_volume = risk_amount / loss_per_lot_account
        volume_step = self.symbol_info.volume_step
        if volume_step <= 0: self.log.warning("Volume step invalide."); adjusted_volume = raw_volume
        else: adjusted_volume = math.floor(raw_volume / volume_step) * volume_step
        final_volume = max(0, min(self.symbol_info.volume_max, adjusted_volume))
        self.log.debug(f"Volume Calculé: {raw_volume:.6f} -> Ajusté Step: {adjusted_volume:.6f} -> Final: {final_volume:.4f}")
        self.log.debug("--- FIN CALCUL VOLUME ---")
        return final_volume

    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        # ... (inchangé) ...
        sl = 0.0; tp = 0.0; direction = trade_signal['direction']
        tp_strategy = self.risk_settings.get('tp_strategy', 'ATR_MULTIPLE') # Utilise tp_strategy de config
        atr = self.calculate_atr(ohlc_data, self.risk_settings.get('atr_settings', {}).get('default', {}).get('period', 14))
        sl_calculated_structurally = False
        if self.sl_strategy == "SMC_STRUCTURE":
            sl_structure_price = trade_signal.get('sl_structure_price')
            if sl_structure_price and atr and atr > 0:
                sl_buffer = self.sl_buffer_pips * self.point
                potential_sl = sl_structure_price - sl_buffer if direction == BUY else sl_structure_price + sl_buffer
                min_sl_dist = atr * 0.5
                if abs(price - potential_sl) >= min_sl_dist:
                    temp_volume = self._calculate_volume(self.account_info.equity, self.risk_settings.get('risk_per_trade', 0.01), price, potential_sl)
                    if temp_volume >= self.symbol_info.volume_min:
                        sl = potential_sl; sl_calculated_structurally = True
                        self.log.debug(f"SL Structurel: SL={sl:.{self.digits}f}")
                    else: self.log.warning(f"SL Structurel -> Vol ({temp_volume:.4f}) < min. Fallback ATR.")
                else: self.log.warning(f"SL Structurel trop proche. Fallback ATR.")
            else: self.log.warning("SL Structurel invalide/manquant ou ATR invalide. Fallback ATR.")
        if not sl_calculated_structurally:
            if atr is None or atr <= 0: return 0.0, 0.0
            sl_multiple = self.risk_settings.get('atr_settings', {}).get('default', {}).get('sl_multiple', 1.5)
            sl = price - (atr * sl_multiple) if direction == BUY else price + (atr * sl_multiple)
            self.log.debug(f"SL ATR: SL={sl:.{self.digits}f}")
        tp_calculated = False
        if tp_strategy == "SMC_LIQUIDITY_TARGET":
            target_price = trade_signal.get('target_price')
            if target_price:
                is_valid = (direction == BUY and target_price > price) or (direction == SELL and target_price < price)
                if is_valid:
                    tp_offset = self.point * 10
                    tp = target_price - tp_offset if direction == BUY else target_price + tp_offset
                    self.log.debug(f"TP SMC: Cible {target_price:.{self.digits}f} -> TP={tp:.{self.digits}f}")
                    tp_calculated = True
                else: self.log.warning(f"TP SMC: Cible invalide vs Prix. Fallback ATR.")
            else: self.log.warning("TP SMC choisi mais cible manquante. Fallback ATR.")
        if not tp_calculated:
            if atr is None or atr <= 0: return sl, 0.0
            tp_multiple = self.risk_settings.get('atr_settings', {}).get('default', {}).get('tp_multiple', 3.0)
            tp = price + (atr * tp_multiple) if direction == BUY else price - (atr * tp_multiple)
            self.log.debug(f"TP ATR: TP={tp:.{self.digits}f}")
        if sl > 0 and tp > 0 and abs(tp - price) < abs(sl - price):
            self.log.warning(f"TP final ({tp:.{self.digits}f}) plus proche que SL final ({sl:.{self.digits}f}) (Ratio < 1).")
        return round(sl, self.digits), round(tp, self.digits)

    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        # ... (inchangé) ...
        if from_currency == to_currency: return 1.0
        pair1 = f"{from_currency}{to_currency}"; info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.ask > 0: return info1.ask
        pair2 = f"{to_currency}{from_currency}"; info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.bid > 0: return 1.0 / info2.bid
        for pivot in ["USD", "EUR", "GBP"]:
             if from_currency != pivot and to_currency != pivot:
                 rate1 = self.get_conversion_rate(from_currency, pivot); rate2 = self.get_conversion_rate(pivot, to_currency)
                 if rate1 and rate2: return rate1 * rate2
        self.log.error(f"Impossible trouver conversion {from_currency} -> {to_currency}"); return None

    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        # ... (inchangé) ...
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
        """Gère les PTP, BE et TSL pour les positions ouvertes."""
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return

        # --- MODIFICATION: Appel PTP en premier ---
        if self.partial_tp_config.get('enabled', False):
            self._apply_partial_take_profit(positions, current_tick)

        # Appliquer BE et TSL ensuite
        if self.risk_settings.get('breakeven', {}).get('enabled', False):
            self._apply_breakeven(positions, current_tick, self.risk_settings.get('breakeven', {}))

        if self.risk_settings.get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, self.risk_settings)

    # --- NOUVELLE FONCTION ---
    def _apply_partial_take_profit(self, positions: list, tick):
        """Vérifie et exécute les prises de profits partielles."""
        levels = self.partial_tp_config.get('levels', [])
        if not levels: return

        for pos in positions:
            context = self._executor._trade_context.get(pos.ticket)
            if not context or pos.sl <= 0: # Si pas de contexte ou SL invalide, on ignore
                continue

            # Vérifier si la position existe toujours (au cas où elle aurait été clôturée entre-temps)
            current_pos_info = self._executor._mt5.positions_get(ticket=pos.ticket)
            if not current_pos_info: continue # Position n'existe plus
            current_pos = current_pos_info[0] # Prendre la première (et unique) position

            # Récupérer l'état actuel depuis le contexte
            initial_volume = context.get('initial_volume', 0)
            remaining_volume = context.get('remaining_volume', current_pos.volume) # Utiliser le volume MT5 comme fallback
            partial_tp_state = context.get('partial_tp_state', [])
            
            if initial_volume <= 0 or len(partial_tp_state) != len(levels):
                 self.log.error(f"Contexte PTP invalide pour #{pos.ticket}. Ignoré.")
                 continue

            risk_distance = abs(pos.price_open - pos.sl) # Distance de risque initiale en prix
            if risk_distance < self.point * 2: continue # SL trop proche

            pnl_price = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY: pnl_price = tick.bid - pos.price_open
            elif pos.type == mt5.ORDER_TYPE_SELL: pnl_price = pos.price_open - tick.ask

            first_ptp_hit_this_cycle = False # Flag pour savoir si on a touché le TP1

            for i, level in enumerate(levels):
                if not partial_tp_state[i]: # Si ce niveau n'a pas encore été traité
                    target_distance = risk_distance * level.get('rr', 0)
                    percentage_to_close = level.get('percentage', 0)

                    if target_distance > 0 and percentage_to_close > 0 and pnl_price >= target_distance:
                        # Niveau PTP atteint !
                        self.log.info(f"PTP #{i+1} (RR {level['rr']}) atteint pour position #{pos.ticket}.")

                        # Calculer le volume absolu à fermer basé sur le volume initial
                        absolute_volume_to_close = initial_volume * (percentage_to_close / 100.0)

                        # Arrondir et valider le volume à fermer
                        symbol_info = self._executor._mt5.symbol_info(pos.symbol) # Re-récupérer infos symbole
                        if not symbol_info: continue
                        vol_step = symbol_info.volume_step
                        vol_min = symbol_info.volume_min
                        vol_digits = symbol_info.volume_digits # Nb décimales pour volume

                        if vol_step > 0:
                            volume_to_close_adjusted = math.floor(absolute_volume_to_close / vol_step) * vol_step
                        else:
                            volume_to_close_adjusted = absolute_volume_to_close

                        # S'assurer de ne pas fermer plus que ce qui reste et d'arrondir correctement
                        volume_to_close_final = round(min(volume_to_close_adjusted, remaining_volume), vol_digits)

                        self.log.debug(f"PTP #{i+1} pour #{pos.ticket}: Initial={initial_volume}, Restant={remaining_volume}, "
                                       f"%={percentage_to_close}, Absolu={absolute_volume_to_close:.6f}, "
                                       f"Ajusté Step={volume_to_close_adjusted:.6f}, Final à fermer={volume_to_close_final:.4f}")

                        if volume_to_close_final >= vol_min:
                            if self._executor.close_partial_position(current_pos, volume_to_close_final):
                                # Succès de la clôture partielle
                                context['remaining_volume'] = round(remaining_volume - volume_to_close_final, vol_digits)
                                context['partial_tp_state'][i] = True
                                self.log.info(f"PTP #{i+1} exécuté pour #{pos.ticket}. Volume restant: {context['remaining_volume']:.4f}")
                                
                                # Si c'est le premier PTP (i=0) et configuré, on met à BE+
                                if i == 0 and self.partial_tp_config.get('move_sl_to_be_after_tp1', False):
                                    first_ptp_hit_this_cycle = True
                            else:
                                # Échec de la clôture partielle, on arrête pour ce cycle pour éviter problèmes
                                self.log.error(f"Échec de l'exécution PTP #{i+1} pour #{pos.ticket}.")
                                break # Sortir de la boucle des niveaux PTP pour cette position
                        else:
                            self.log.warning(f"Volume PTP #{i+1} ({volume_to_close_final:.4f}) < min ({vol_min}). PTP ignoré.")
                            # Marquer comme traité pour ne pas réessayer ? Non, on réessaiera si le prix reste là.

            # Appliquer le BE+ après TP1 si activé et réussi ce cycle
            if first_ptp_hit_this_cycle:
                 pips_plus = self.partial_tp_config.get('be_pips_plus_after_tp1', 5)
                 be_sl_price = 0.0
                 if pos.type == mt5.ORDER_TYPE_BUY:
                     be_sl_price = pos.price_open + (pips_plus * self.point)
                 elif pos.type == mt5.ORDER_TYPE_SELL:
                     be_sl_price = pos.price_open - (pips_plus * self.point)
                 
                 # Vérifier si ce nouveau SL est meilleur que l'actuel
                 if (pos.type == mt5.ORDER_TYPE_BUY and be_sl_price > pos.sl) or \
                    (pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or be_sl_price < pos.sl)):
                      self.log.info(f"Application BE+{pips_plus} pips après PTP1 pour #{pos.ticket}. New SL: {be_sl_price:.{self.digits}f}")
                      self._executor.modify_position(pos.ticket, be_sl_price, pos.tp)


    # --- FIN NOUVELLE FONCTION ---

    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        # ... (Logique inchangée mais ne s'applique que si PTP n'a pas déjà mis à BE) ...
        trigger_pips = be_cfg.get('trigger_pips', 150)
        pips_plus = be_cfg.get('pips_plus', 10)
        for pos in positions:
             # Vérifier si PTP a déjà mis à BE (simple check si SL = BE+)
            context = self._executor._trade_context.get(pos.ticket)
            be_pips_plus_ptp1 = self.partial_tp_config.get('be_pips_plus_after_tp1', 5)
            be_sl_price_ptp1 = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY: be_sl_price_ptp1 = pos.price_open + (be_pips_plus_ptp1 * self.point)
            elif pos.type == mt5.ORDER_TYPE_SELL: be_sl_price_ptp1 = pos.price_open - (be_pips_plus_ptp1 * self.point)
            
            # Si le SL actuel est déjà au niveau BE+ de PTP1, ne pas appliquer le BE normal
            if pos.sl != 0 and abs(pos.sl - be_sl_price_ptp1) < self.point:
                 continue

            pnl_pips = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_pips = (tick.bid - pos.price_open) / self.point
                if pos.sl != 0 and pos.sl < pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open + (pips_plus * self.point)
                    # S'assurer que le BE normal est meilleur que le SL actuel
                    if breakeven_sl > pos.sl:
                         self.log.info(f"BE déclenché #{pos.ticket}. New SL: {breakeven_sl:.{self.digits}f}")
                         self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_pips = (pos.price_open - tick.ask) / self.point
                if pos.sl != 0 and pos.sl > pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open - (pips_plus * self.point)
                    # S'assurer que le BE normal est meilleur que le SL actuel
                    if pos.sl == 0 or breakeven_sl < pos.sl:
                         self.log.info(f"BE déclenché #{pos.ticket}. New SL: {breakeven_sl:.{self.digits}f}")
                         self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)

    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        # ... (inchangé) ...
        ts_cfg = risk_cfg.get('trailing_stop_atr', {})
        atr_cfg = risk_cfg.get('atr_settings', {}).get('default', {})
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
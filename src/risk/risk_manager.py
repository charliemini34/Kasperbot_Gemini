# Fichier: src/risk/risk_manager.py
# Version: 1.3.2 (Implémentation Sugg 1, 7)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, time, src.constants
# DESCRIPTION: Ajout Sugg 1 (Vérif TP min dist) et Sugg 7 (TSL Structure).

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta # Ajout timedelta pour cache
import pytz
import time # Ajouté pour cache
from typing import Tuple, List, Dict, Optional

from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque.
    v1.3.2: Ajout Sugg 1 (Vérif TP min dist), Sugg 7 (TSL Structure).
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config: Dict = config
        self._executor = executor
        self._symbol: str = symbol

        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()

        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les informations pour {self._symbol} ou compte.")
            raise ValueError("Informations de compte ou de symbole MT5 manquantes.")

        self.point: float = self.symbol_info.point
        self.digits: int = self.symbol_info.digits
        self._partial_tp_taken = {}

        # --- [Optimisation 1] Cache Taux de Change ---
        self._conversion_rate_cache: Dict[str, Tuple[float, float]] = {}
        self._cache_duration: timedelta = timedelta(seconds=60) # Validité du cache (ex: 60 secondes)
        # --- Fin [Optimisation 1] ---

    def is_daily_loss_limit_reached(self) -> Tuple[bool, float]:
        # (Logique inchangée)
        risk_settings = self._config.get('risk_management', {})
        loss_limit_percent = risk_settings.get('daily_loss_limit_percent', 5.0)
        if loss_limit_percent <= 0: return False, 0.0
        try:
            today_start_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            now_utc = datetime.now(pytz.utc)
            history_deals = self._executor._mt5.history_deals_get(today_start_utc, now_utc)
            if history_deals is None: return False, 0.0
            magic_number = self._config.get('trading_settings', {}).get('magic_number', 0)
            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == magic_number and deal.entry in [mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT])
            equity_now = self.account_info.equity # Utiliser equity actuelle pour calcul limite
            loss_limit_amount = (equity_now * loss_limit_percent) / 100.0
            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) atteint limite {loss_limit_percent}%.")
                return True, daily_pnl
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur calcul limite perte jour : {e}", exc_info=True)
            return False, 0.0

    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        # (Logique inchangée - Sugg 3.2 déjà implémentée)
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal invalide: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0
                
            rm_settings = self._config.get('risk_management', {})
            risk_percent_from_config = rm_settings.get('risk_per_trade', 1.0)
            risk_percent = risk_percent_from_config / 100.0
            min_rr = rm_settings.get('min_rr', 2.0)

            if risk_percent <= 0:
                 self.log.error(f"Risque par trade ({risk_percent_from_config}%) <= 0.")
                 return 0.0, 0.0, 0.0

            # Utilise la fonction modifiée (Sugg 1)
            ideal_sl, ideal_tp = self._calculate_initial_sl_tp_with_min_dist(price, ohlc_data, trade_signal)
            
            sl_buffer_pips = rm_settings.get('sl_buffer_pips', 0)
            if sl_buffer_pips > 0 and ideal_sl != 0:
                 sl_buffer = sl_buffer_pips * self.point
                 ideal_sl = ideal_sl - sl_buffer if trade_signal['direction'] == BUY else ideal_sl + sl_buffer
                 ideal_sl = round(ideal_sl, self.digits)

            if ideal_sl == 0 or ideal_tp == 0:
                 self.log.error(f"SL/TP final invalide (0). SL: {ideal_sl}, TP: {ideal_tp}")
                 return 0.0, 0.0, 0.0

            sl_distance_final = abs(price - ideal_sl)
            tp_distance_final = abs(ideal_tp - price)
            
            if sl_distance_final < self.point:
                 self.log.error(f"Distance SL finale invalide ({sl_distance_final:.{self.digits}f}).")
                 return 0.0, 0.0, 0.0
                 
            calculated_rr = tp_distance_final / sl_distance_final
            if calculated_rr < min_rr:
                self.log.warning(f"Trade annulé. RR Calculé ({calculated_rr:.2f}) < Min ({min_rr}). SL={ideal_sl:.{self.digits}f}, TP={ideal_tp:.{self.digits}f}")
                return 0.0, 0.0, 0.0

            ideal_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)

            if ideal_volume <= 0:
                 self.log.warning(f"Volume calculé nul ({ideal_volume:.4f}). SL={ideal_sl:.{self.digits}f}.")
                 return 0.0, 0.0, 0.0
            if ideal_volume < self.symbol_info.volume_min:
                self.log.warning(f"Volume idéal ({ideal_volume:.4f}) < Min ({self.symbol_info.volume_min}). SL={ideal_sl:.{self.digits}f}.")
                return 0.0, 0.0, 0.0

            self.log.info(f"Paramètres validés: Vol={ideal_volume:.2f}, SL={ideal_sl:.{self.digits}f}, TP={ideal_tp:.{self.digits}f}, RR={calculated_rr:.2f}")
            return ideal_volume, ideal_sl, ideal_tp

        except Exception as e:
            self.log.error(f"Erreur calculate_trade_parameters : {e}", exc_info=True)
            return 0.0, 0.0, 0.0

    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        # (Logique inchangée mais utilise get_conversion_rate avec cache)
        risk_amount_account_currency = equity * risk_percent
        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point: return 0.0
        loss_per_lot_profit_currency = sl_distance_price * self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if profit_currency != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(profit_currency, self.account_info.currency) # Utilise le cache
            if not conversion_rate or conversion_rate <= 0:
                self.log.error(f"Échec obtention taux de change pour {profit_currency}->{self.account_info.currency}")
                return 0.0
            loss_per_lot_account_currency *= conversion_rate
        if loss_per_lot_account_currency <= 0: return 0.0
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        volume_step = self.symbol_info.volume_step
        if volume_step <= 0: return 0.0
        volume = math.floor(volume / volume_step) * volume_step
        volume = round(volume, 8)
        return max(0.0, min(self.symbol_info.volume_max, volume))

    def _find_swing_points(self, df: pd.DataFrame, n: int = 3):
        # (Logique inchangée)
        window_size = n * 2 + 1
        df_historical = df.iloc[:-1]
        if 'is_swing_high' not in df.columns: df['is_swing_high'] = False
        if 'is_swing_low' not in df.columns: df['is_swing_low'] = False
        if len(df_historical) >= window_size:
             # Utiliser center=True pour une détection standard de swing
             high_swings = df_historical['high'].rolling(window=window_size, center=True, min_periods=window_size).max() == df_historical['high']
             low_swings = df_historical['low'].rolling(window=window_size, center=True, min_periods=window_size).min() == df_historical['low']
             df.loc[high_swings.index, 'is_swing_high'] = high_swings
             df.loc[low_swings.index, 'is_swing_low'] = low_swings
        return df[df['is_swing_high'] == True], df[df['is_swing_low'] == True]

    # --- MODIFICATION SUGGESTION 1 ---
    def _calculate_initial_sl_tp_with_min_dist(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        """
        Calcule SL/TP et applique la distance minimale (trade_stops_level) au SL et au TP.
        """
        ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)
        if ideal_sl == 0 or ideal_tp == 0: return 0.0, 0.0
        
        direction = trade_signal['direction']
        
        # Ajouter 2 points de buffer au stops_level pour sécurité
        min_distance_points = self.symbol_info.trade_stops_level + 2
        min_distance_price = min_distance_points * self.point

        # Vérification SL (inchangée)
        current_sl_distance = abs(price - ideal_sl)
        if current_sl_distance < min_distance_price:
            self.log.warning(f"SL initial ({ideal_sl:.{self.digits}f}) trop proche. Ajustement à dist min ({min_distance_price:.{self.digits}f}).")
            if direction == BUY: ideal_sl = price - min_distance_price
            elif direction == SELL: ideal_sl = price + min_distance_price
            ideal_sl = round(ideal_sl, self.digits)
            
        # Vérification TP (AJOUT SUGG 1)
        current_tp_distance = abs(ideal_tp - price)
        if current_tp_distance < min_distance_price:
            self.log.warning(f"TP initial ({ideal_tp:.{self.digits}f}) trop proche. Ajustement à dist min ({min_distance_price:.{self.digits}f}).")
            if direction == BUY: ideal_tp = price + min_distance_price
            elif direction == SELL: ideal_tp = price - min_distance_price
            ideal_tp = round(ideal_tp, self.digits)

        return ideal_sl, ideal_tp
    # --- FIN MODIFICATION SUGGESTION 1 ---

    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        # (Logique inchangée)
        rm_settings = self._config.get('risk_management', {})
        sl_strategy = rm_settings.get('sl_strategy', 'ATR_MULTIPLE')
        tp_strategy = rm_settings.get('tp_strategy', 'SMC_LIQUIDITY_TARGET')
        direction = trade_signal['direction']
        atr_settings_key = self._symbol
        atr_settings = rm_settings.get('atr_settings', {}).get(atr_settings_key, rm_settings.get('atr_settings', {}).get('default', {}))
        atr = self.calculate_atr(ohlc_data, atr_settings.get('period', 14))
        if atr is None or atr <= 0: 
            self.log.error(f"ATR invalide ({atr}) pour {self._symbol}. Impossible de calculer SL/TP.")
            return 0.0, 0.0
        sl = 0.0; tp = 0.0
        sl_distance_atr_fallback = atr * atr_settings.get('sl_multiple', 1.5)
        tp_distance_atr_fallback = atr * atr_settings.get('tp_multiple', 3.0)
        # Calcul SL (basé sur LTF)
        if sl_strategy == "SMC_STRUCTURE":
            swing_highs, swing_lows = self._find_swing_points(ohlc_data.copy(), n=3) # Utiliser .copy()
            try:
                if direction == BUY:
                    relevant_lows = swing_lows[swing_lows['low'] < price]
                    sl = relevant_lows['low'].iloc[-1] if not relevant_lows.empty else price - sl_distance_atr_fallback
                elif direction == SELL:
                    relevant_highs = swing_highs[swing_highs['high'] > price]
                    sl = relevant_highs['high'].iloc[-1] if not relevant_highs.empty else price + sl_distance_atr_fallback
            except Exception as e:
                 self.log.error(f"Erreur SL SMC (LTF): {e}. Fallback ATR.", exc_info=False)
                 sl = price - sl_distance_atr_fallback if direction == BUY else price + sl_distance_atr_fallback
        else: sl = price - sl_distance_atr_fallback if direction == BUY else price + sl_distance_atr_fallback
        if sl == 0: return 0.0, 0.0
        # Calcul TP (utilise target_price HTF)
        use_atr_fallback_for_tp = False
        if tp_strategy == "SMC_LIQUIDITY_TARGET":
            tp = trade_signal.get('target_price')
            if not tp or tp == 0: use_atr_fallback_for_tp = True
            elif (direction == BUY and tp < price) or (direction == SELL and tp > price): use_atr_fallback_for_tp = True
        else: use_atr_fallback_for_tp = True
        if use_atr_fallback_for_tp:
            tp = price + tp_distance_atr_fallback if direction == BUY else price - tp_distance_atr_fallback
        if tp == 0: return 0.0, 0.0
        return round(sl, self.digits), round(tp, self.digits)

    # --- [Optimisation 1] get_conversion_rate avec cache ---
    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        """ Récupère le taux de change, en utilisant un cache. """
        if from_currency == to_currency: return 1.0

        cache_key = f"{from_currency}->{to_currency}"
        current_time = time.monotonic()

        # Vérifier le cache
        if cache_key in self._conversion_rate_cache:
            rate, timestamp = self._conversion_rate_cache[cache_key]
            if current_time - timestamp < self._cache_duration.total_seconds():
                self.log.debug(f"Cache HIT pour {cache_key}: {rate}")
                return rate
            else:
                self.log.debug(f"Cache EXPIRED pour {cache_key}")
                del self._conversion_rate_cache[cache_key] # Supprimer l'entrée expirée

        self.log.debug(f"Cache MISS pour {cache_key}. Récupération via API MT5...")
        rate = self._fetch_conversion_rate_from_mt5(from_currency, to_currency)

        # Mettre à jour le cache si un taux valide est trouvé
        if rate is not None and rate > 0:
            self._conversion_rate_cache[cache_key] = (rate, current_time)
            self.log.debug(f"Cache SET pour {cache_key}: {rate}")

        return rate

    def _fetch_conversion_rate_from_mt5(self, from_currency: str, to_currency: str) -> Optional[float]:
        """ Logique originale de récupération du taux via MT5. """
        # Essai direct
        pair1 = f"{from_currency}{to_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.ask > 0: return info1.ask
        # Essai inversé
        pair2 = f"{to_currency}{from_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.bid > 0: return 1.0 / info2.bid
        # Essai triangulation via pivots
        for pivot in ["USD", "EUR", "GBP"]:
             if from_currency != pivot and to_currency != pivot:
                 rate1 = self._get_rate_or_inverse(from_currency, pivot)
                 rate2 = self._get_rate_or_inverse(pivot, to_currency)
                 if rate1 > 0 and rate2 > 0: return rate1 * rate2
        self.log.error(f"Conversion impossible via MT5: {from_currency} -> {to_currency}")
        return None

    def _get_rate_or_inverse(self, curr1: str, curr2: str) -> float:
        """ Helper pour _fetch_conversion_rate_from_mt5. """
        pair_direct = f"{curr1}{curr2}"
        info_direct = self._executor._mt5.symbol_info_tick(pair_direct)
        if info_direct and info_direct.ask > 0: return info_direct.ask
        pair_inverse = f"{curr2}{curr1}"
        info_inverse = self._executor._mt5.symbol_info_tick(pair_inverse)
        if info_inverse and info_inverse.bid > 0: return 1.0 / info_inverse.bid
        return 0.0
    # --- Fin [Optimisation 1] ---

    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> Optional[float]:
        # (Logique inchangée)
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period + 1: return None
        try:
             high_low = ohlc_data['high'] - ohlc_data['low']
             high_close = np.abs(ohlc_data['high'] - ohlc_data['close'].shift())
             low_close = np.abs(ohlc_data['low'] - ohlc_data['close'].shift())
             ranges = pd.concat([high_low, high_close, low_close], axis=1)
             true_range = np.max(ranges, axis=1)
             atr = true_range.ewm(span=period, adjust=False).mean().iloc[-1]
             if pd.isna(atr) or atr <= 0: return None
             return atr
        except Exception: return None

    # --- MODIFICATION SUGGESTION 7 ---
    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        """ Gère les TPs partiels, BE, et Trailing Stops (ATR ou Structure). """
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return []
        
        partial_close_actions = []
        risk_settings = self._config.get('risk_management', {})

        # 1. TPs Partiels
        if risk_settings.get('partial_tp', {}).get('enabled', False):
            actions = self._apply_partial_tp(positions, current_tick, risk_settings.get('partial_tp', {}))
            partial_close_actions.extend(actions)
            
        # 2. Breakeven
        if risk_settings.get('breakeven', {}).get('enabled', False):
            self._apply_breakeven(positions, current_tick, risk_settings.get('breakeven', {}))
            
        # 3. Trailing Stop (Logique 'elif' pour n'en activer qu'un seul)
        ts_atr_cfg = risk_settings.get('trailing_stop_atr', {})
        ts_smc_cfg = risk_settings.get('trailing_stop_structure', {})

        if ts_atr_cfg.get('enabled', False):
            self.log.debug("Application Trailing Stop ATR")
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, risk_settings)
        elif ts_smc_cfg.get('enabled', False):
            self.log.debug("Application Trailing Stop Structure (SMC)")
            # Doit passer une copie de ohlc_data pour éviter SettingWithCopyWarning
            self._apply_trailing_stop_structure(positions, ohlc_data.copy(), ts_smc_cfg)
            
        return partial_close_actions
    # --- FIN MODIFICATION SUGGESTION 7 ---

    def _apply_partial_tp(self, positions: list, tick, partial_cfg: dict):
        # (Logique inchangée)
        actions = []
        levels = partial_cfg.get('levels', [])
        if not levels: return actions
        levels.sort(key=lambda x: x.get('rr', 0))
        for pos in positions:
            if pos.sl == 0: continue
            initial_risk_pips = abs(pos.price_open - pos.sl) / self.point
            if initial_risk_pips <= 0: continue
            current_pnl_pips = 0
            if pos.type == mt5.ORDER_TYPE_BUY: current_pnl_pips = (tick.bid - pos.price_open) / self.point
            elif pos.type == mt5.ORDER_TYPE_SELL: current_pnl_pips = (pos.price_open - tick.ask) / self.point
            current_rr = current_pnl_pips / initial_risk_pips if initial_risk_pips > 0 else 0
            if pos.ticket not in self._partial_tp_taken: self._partial_tp_taken[pos.ticket] = set()
            taken_levels = self._partial_tp_taken[pos.ticket]
            context = next((ctx for order_id, ctx in self._executor._trade_context.items() if ctx.get('position_id') == pos.ticket), None)
            if not context: continue
            initial_volume = context.get('volume_initial', pos.volume)
            for level_cfg in levels:
                target_rr = level_cfg.get('rr')
                percentage_to_close = level_cfg.get('percentage') / 100.0
                if target_rr is not None and current_rr >= target_rr and target_rr not in taken_levels:
                    volume_to_close = initial_volume * percentage_to_close
                    volume_step = self.symbol_info.volume_step
                    if volume_step > 0: volume_to_close = math.floor(volume_to_close / volume_step) * volume_step; volume_to_close = round(volume_to_close, 8)
                    volume_to_close = min(volume_to_close, pos.volume)
                    if volume_to_close >= self.symbol_info.volume_min:
                        actions.append({'ticket': pos.ticket, 'volume': volume_to_close, 'trade_id': f"TP{target_rr}R"})
                        taken_levels.add(target_rr)
                        if target_rr == levels[0].get('rr') and partial_cfg.get('move_sl_to_be_after_tp1', False):
                            be_pips = partial_cfg.get('be_pips_plus_after_tp1', 0)
                            breakeven_sl = pos.price_open + (be_pips * self.point) if pos.type == mt5.ORDER_TYPE_BUY else pos.price_open - (be_pips * self.point)
                            should_move_sl = (pos.type == mt5.ORDER_TYPE_BUY and breakeven_sl > pos.sl) or \
                                             (pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or breakeven_sl < pos.sl))
                            if should_move_sl: self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp, trade_id=f"BE_after_TP{target_rr}R")
                    else: taken_levels.add(target_rr) # Marquer comme pris même si volume trop faible
        return actions

    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        # (Logique inchangée)
        trigger_pips = be_cfg.get('trigger_pips', 100); pips_plus = be_cfg.get('pips_plus', 10)
        trigger_distance = trigger_pips * self.point; be_adjustment = pips_plus * self.point
        for pos in positions:
            move_sl = False; breakeven_sl = pos.sl
            if pos.type == mt5.ORDER_TYPE_BUY and (tick.bid - pos.price_open) >= trigger_distance:
                potential_be_sl = pos.price_open + be_adjustment
                if potential_be_sl > pos.sl: move_sl = True; breakeven_sl = potential_be_sl
            elif pos.type == mt5.ORDER_TYPE_SELL and (pos.price_open - tick.ask) >= trigger_distance:
                potential_be_sl = pos.price_open - be_adjustment
                if pos.sl == 0 or potential_be_sl < pos.sl: move_sl = True; breakeven_sl = potential_be_sl
            if move_sl: self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp, trade_id="BE")

    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        # (Logique inchangée)
        ts_cfg = risk_cfg.get('trailing_stop_atr', {}); atr_settings_key = self._symbol
        atr_cfg = risk_cfg.get('atr_settings', {}).get(atr_settings_key, risk_cfg.get('atr_settings', {}).get('default', {}))
        period = atr_cfg.get('period', 14); atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0: return
        activation_multiple = ts_cfg.get('activation_multiple', 2.0); trailing_multiple = ts_cfg.get('trailing_multiple', 1.8)
        activation_distance = atr * activation_multiple; trailing_distance = atr * trailing_multiple
        for pos in positions:
            move_sl = False; new_sl = pos.sl
            if pos.type == mt5.ORDER_TYPE_BUY and (tick.bid - pos.price_open) >= activation_distance:
                potential_new_sl = tick.bid - trailing_distance
                if potential_new_sl > pos.sl: new_sl = potential_new_sl; move_sl = True
            elif pos.type == mt5.ORDER_TYPE_SELL and (pos.price_open - tick.ask) >= activation_distance:
                potential_new_sl = tick.ask + trailing_distance
                if pos.sl == 0 or potential_new_sl < pos.sl: new_sl = potential_new_sl; move_sl = True
            if move_sl:
                rounded_new_sl = round(new_sl, self.digits)
                if rounded_new_sl != round(pos.sl, self.digits): self._executor.modify_position(pos.ticket, rounded_new_sl, pos.tp, trade_id="TS_ATR")

    # --- AJOUT SUGGESTION 7 ---
    def _apply_trailing_stop_structure(self, positions: list, ohlc_data: pd.DataFrame, ts_smc_cfg: dict):
        """
        Applique un trailing stop basé sur la structure LTF (derniers swings).
        """
        period = ts_smc_cfg.get('ltf_swing_period', 3)
        
        # Note: ohlc_data devrait être une copie pour éviter SettingWithCopyWarning
        swing_highs, swing_lows = self._find_swing_points(ohlc_data, n=period)

        if swing_lows.empty and swing_highs.empty:
            self.log.debug(f"TSL Structure: Pas de swings LTF trouvés pour {self._symbol}.")
            return

        for pos in positions:
            move_sl = False
            new_sl = pos.sl
            pos_open_time = datetime.fromtimestamp(pos.time, tz=pytz.utc)

            try:
                if pos.type == mt5.ORDER_TYPE_BUY:
                    # Trailing Buy: SL sous le dernier swing low LTF
                    # On ne prend que les lows formés *après* l'ouverture
                    relevant_lows = swing_lows[swing_lows.index > pos_open_time]
                    if not relevant_lows.empty:
                        potential_new_sl = relevant_lows['low'].iloc[-1]
                        # SL ne doit jamais reculer
                        if potential_new_sl > pos.sl:
                            new_sl = potential_new_sl
                            move_sl = True
                
                elif pos.type == mt5.ORDER_TYPE_SELL:
                    # Trailing Sell: SL au-dessus du dernier swing high LTF
                    relevant_highs = swing_highs[swing_highs.index > pos_open_time]
                    if not relevant_highs.empty:
                        potential_new_sl = relevant_highs['high'].iloc[-1]
                        # SL ne doit jamais reculer
                        if pos.sl == 0 or potential_new_sl < pos.sl:
                            new_sl = potential_new_sl
                            move_sl = True
            
            except Exception as e:
                self.log.warning(f"[{pos.ticket}] Erreur TSL Structure: {e}", exc_info=False)
                continue

            if move_sl:
                # Appliquer le buffer de SL global
                sl_buffer_pips = self._config.get('risk_management', {}).get('sl_buffer_pips', 0)
                if sl_buffer_pips > 0:
                    sl_buffer = sl_buffer_pips * self.point
                    if pos.type == mt5.ORDER_TYPE_BUY:
                        new_sl = new_sl - sl_buffer
                    else:
                        new_sl = new_sl + sl_buffer
                
                rounded_new_sl = round(new_sl, self.digits)

                if rounded_new_sl != round(pos.sl, self.digits):
                    self.log.info(f"[{pos.ticket}] TSL Structure: Déplacement SL à {rounded_new_sl:.{self.digits}f}")
                    self._executor.modify_position(pos.ticket, rounded_new_sl, pos.tp, trade_id="TS_SMC")
    # --- FIN AJOUT SUGGESTION 7 ---
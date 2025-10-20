# Fichier: src/risk/risk_manager.py
# Version: 18.1.9 (Critical-Hotfix-Bundle)
# Dépendances: MetaTrader5, pandas, numpy, logging, decimal, pytz, datetime, typing
# Description: Corrige 'is_daily_loss_limit_reached', 'tick_value', 'SYMBOL_CALC_MODE_FX', et 'TYPE_CHECKING'.

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, time as dt_time, timedelta
import pytz
from typing import Tuple, Optional, Dict, List, TYPE_CHECKING # Import complet

from src.constants import BUY, SELL

if TYPE_CHECKING:
    from src.execution.mt5_executor import MT5Executor

class RiskManager:
    """
    Gère tous les aspects du risque.
    v18.1.9: Hotfix pour 'is_daily_loss_limit_reached' manquant, 'tick_value' incorrect,
    'SYMBOL_CALC_MODE_FX' incorrect, et 'TYPE_CHECKING' manquant.
    """

    def __init__(self, config: dict, executor: 'MT5Executor', symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self.executor = executor
        try:
            self.mt5 = executor.get_mt5_connection()
        except AttributeError:
             # Fallback pour v15.4.7 (fourni)
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
        # Note: 'trade_management' n'existe pas dans config v18.3.1, utilise 'risk_management'
        self.ptp_rules = risk_settings.get('partial_tp', {}).get('levels', [])
        self.breakeven_rules = risk_settings.get('breakeven', {})
        self.trailing_stop_rules = risk_settings.get('trailing_stop_atr', {})

        # Paramètres SL/TP
        self.sl_strategy = risk_settings.get('sl_strategy', 'ATR_MULTIPLE')
        self.tp_strategy = risk_settings.get('tp_strategy', 'SMC_LIQUIDITY_TARGET')
        self.sl_buffer_pips = Decimal(str(risk_settings.get('sl_buffer_pips', 1.0)))
        self.tp_buffer_pips = Decimal(str(risk_settings.get('tp_buffer_pips', 0.0)))
        
        atr_settings = risk_settings.get('atr_settings', {})
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
        
        # --- CORRECTION : Attributs MT5 corrects ---
        self.trade_tick_value = self.symbol_info.trade_tick_value
        self.trade_tick_size = self.symbol_info.trade_tick_size
        # --- FIN CORRECTION ---

        self.account_currency = self.account_info.currency
        self.ohlc_data_cache = None


    def _calculate_atr(self, ohlc_data: pd.DataFrame, period: int = 14) -> float:
        if ohlc_data is None or len(ohlc_data) < period:
            logging.warning(f"Données OHLC insuffisantes pour ATR({period}) sur {self.symbol}")
            return 0.0
        try:
            high_low = ohlc_data['high'].astype(float) - ohlc_data['low'].astype(float)
            high_close = np.abs(ohlc_data['high'].astype(float) - ohlc_data['close'].astype(float).shift())
            low_close = np.abs(ohlc_data['low'].astype(float) - ohlc_data['close'].astype(float).shift())
        except Exception as e:
             logging.error(f"Erreur conversion type pour ATR: {e}"); return 0.0
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        if pd.isna(atr) or atr == 0.0:
             logging.warning(f"Calcul ATR invalide (NaN ou 0.0) pour {self.symbol}. Fallback 10 pips.")
             return self.point * 10
        return atr


    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Decimal:
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
                 pip_value_profit_currency = point_d * contract_size_d

            elif calc_mode in [mt5.SYMBOL_CALC_MODE_CFD, mt5.SYMBOL_CALC_MODE_CFDINDEX, mt5.SYMBOL_CALC_MODE_FUTURES, mt5.SYMBOL_CALC_MODE_CFDLEVERAGE]:
                 if tick_size_d == 0: logging.error(f"{self.symbol}: Tick size est zéro."); return Decimal('0.0')
                 point_value_margin_curr = (point_d / tick_size_d) * tick_value_d * volume
                 if self.currency_margin == self.account_currency:
                      pip_value_profit_currency = point_value_margin_curr
                 else:
                      conversion_rate = self.get_conversion_rate(self.currency_margin, self.account_currency)
                      if conversion_rate <= 0: logging.error(f"Taux conversion invalide {self.currency_margin}->{self.account_currency}"); return Decimal('0.0')
                      pip_value_profit_currency = point_value_margin_curr * conversion_rate
            else:
                logging.warning(f"Mode calcul {calc_mode} non géré. Fallback simple.")
                pip_value_profit_currency = point_d * contract_size_d
            
            if calc_mode not in [mt5.SYMBOL_CALC_MODE_CFD, mt5.SYMBOL_CALC_MODE_CFDINDEX, mt5.SYMBOL_CALC_MODE_FUTURES, mt5.SYMBOL_CALC_MODE_CFDLEVERAGE]:
                 pip_value_profit_currency *= volume

            if self.currency_profit == self.account_currency:
                return pip_value_profit_currency.quantize(Decimal("0.00001"))
            else:
                conversion_rate = self.get_conversion_rate(self.currency_profit, self.account_currency)
                if conversion_rate > 0:
                    return (pip_value_profit_currency * conversion_rate).quantize(Decimal("0.00001"))
                else:
                    logging.error(f"Pip Value: Taux conversion {self.currency_profit}->{self.account_currency} invalide.")
                    return Decimal('0.0')
                    
        except Exception as e:
            self.log.error(f"Erreur inattendue dans _calculate_pip_value: {e}", exc_info=True)
            return Decimal('0.0')


    
    def _calculate_volume(self, equity: Decimal, sl_price: Decimal, entry_price: Decimal, direction: str) -> Decimal:
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
                else: logging.warning(f"SL Structurel {self.symbol} ({potential_sl}) donne volume {temp_vol_d} < min. Fallback ATR.")
            else: logging.warning(f"SL Structurel {self.symbol} ({potential_sl}) trop proche/ATR invalide. Fallback ATR.")
                 
        if not sl_calculated_structurally: # Fallback ATR
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
                potential_tp = target_price - tp_buffer_abs if direction == "BUY" else target_price + tp_buffer_abs # Correction offset SELL
                # Re-valider après buffer
                if (direction == "BUY" and potential_tp > entry_price) or (direction == "SELL" and potential_tp < entry_price):
                     tp_price = potential_tp; tp_calculated_structurally = True
                     logging.debug(f"TP {self.symbol} (SMC_LIQUIDITY_TARGET): {tp_price:.{self.digits}f}")
                else: logging.warning(f"TP SMC {self.symbol} ({potential_tp}) invalide après buffer. Fallback ATR.")
            else: logging.warning(f"TP SMC {self.symbol} cible ({target_price}) non profitable vs entrée ({entry_price}). Fallback ATR.")
        
        if not tp_calculated_structurally: # Fallback ATR
            if atr == 0.0: logging.error("ATR invalide, impossible de calculer TP fallback."); return sl_price, 0.0 # Retourne SL valide
            tp_distance = float(self.tp_atr_multiplier) * atr
            tp_price = entry_price + tp_distance if direction == "BUY" else entry_price - tp_distance
            logging.debug(f"TP {self.symbol} (ATR_MULTIPLE): {tp_price:.{self.digits}f}")

        # 3. Validation finale et Arrondi
        if (direction == "BUY" and (sl_price >= entry_price or tp_price <= entry_price)) or \
           (direction == "SELL" and (sl_price <= entry_price or tp_price >= entry_price)):
             # Si SL est invalide (ex: 0.0), annuler
             if sl_price == 0.0:
                 logging.error(f"Erreur logique SL/TP {self.symbol}: SL est 0. Annulation.")
                 return 0.0, 0.0
             # Si TP est invalide (ex: 0.0), annuler
             if tp_price == 0.0:
                  logging.error(f"Erreur logique SL/TP {self.symbol}: TP est 0. Annulation.")
                  return 0.0, 0.0
             logging.error(f"Erreur logique SL/TP {self.symbol}: E={entry_price}, SL={sl_price}, TP={tp_price}. Annulation.")
             return 0.0, 0.0
             
        sl_price = round(sl_price, self.digits)
        tp_price = round(tp_price, self.digits)
        
        # Vérification finale distance min
        if abs(entry_price - sl_price) < self.point * 5 or abs(entry_price - tp_price) < self.point * 5:
             logging.error(f"SL/TP final trop proche du prix. SL: {sl_price}, TP: {tp_price}, Prix: {entry_price}. Annulation.")
             return 0.0, 0.0
             
        return sl_price, tp_price


    def manage_open_positions(self, positions: list, tick, ohlc_data: pd.DataFrame):
        self.ohlc_data_cache = ohlc_data # Stocker pour TSL
        if not positions: return
        
        # Appliquer PTP en premier
        if self.ptp_rules:
            # Créer une copie de la liste pour itérer car _execute_partial_close modifie la position
            for pos in list(positions): 
                # Re-vérifier si la position existe toujours (elle a pu être fermée par un PTP précédent)
                current_pos_info = self.mt5.positions_get(ticket=pos.ticket)
                if not current_pos_info: continue
                pos_updated = current_pos_info[0] # Utiliser l'état frais

                for rule in self.ptp_rules:
                    rr_target = Decimal(str(rule.get('rr', 1.0)))
                    percentage_to_close = Decimal(str(rule.get('percentage', 50.0))) / Decimal('100.0')
                    # Passer pos_updated
                    self._apply_ptp(pos_updated, tick, rr_target, percentage_to_close)

        # Re-fetch positions après PTP potentiels
        magic_number = self.config['trading_settings'].get('magic_number', 0)
        positions = self.executor.get_open_positions(magic=magic_number)
        if not positions: return

        # Appliquer BE
        if self.breakeven_rules.get('enabled', False):
            if self.breakeven_rules.get('move_to_be_plus_on_ptp1', False) and self.ptp_rules:
                ptp1_rr = Decimal(str(self.ptp_rules[0].get('rr', 1.0)))
                self._apply_breakeven_on_ptp(positions, tick, ptp1_rr)
            else:
                trigger_pips = self.breakeven_rules.get('trigger_pips', 0)
                if trigger_pips > 0: self._apply_breakeven_pips(positions, tick, trigger_pips)

        # Appliquer TSL
        if self.trailing_stop_rules.get('enabled', False):
            activation_multiple = Decimal(str(self.trailing_stop_rules.get('activation_multiple', 2.0)))
            trailing_multiple = Decimal(str(self.trailing_stop_rules.get('trailing_multiple', 1.5)))
            atr = Decimal(str(self._calculate_atr(ohlc_data, 14)))
            if atr > 0: self._apply_trailing_stop_atr(positions, tick, atr, activation_multiple, trailing_multiple)

    def _apply_ptp(self, pos, tick, rr_target: Decimal, percentage_to_close: Decimal):
        # Ne prend qu'une position, pas une liste
        if f"PTP{rr_target}" in pos.comment: return
        
        context = self._executor._trade_context.get(pos.ticket)
        if not context:
             logging.warning(f"PTP: Contexte introuvable pour ticket #{pos.ticket}. PTP ignoré.")
             return
             
        initial_sl = context.get('sl_initial', pos.sl) # Utiliser SL initial du contexte
        if initial_sl <= 0: logging.warning(f"PTP: SL initial invalide (0) pour #{pos.ticket}."); return
        
        initial_entry = pos.price_open
        
        if pos.type == mt5.ORDER_TYPE_BUY:
            sl_distance = Decimal(str(initial_entry - initial_sl))
            if sl_distance <= 0: return
            tp_target = Decimal(str(initial_entry)) + (sl_distance * rr_target)
            current_price = Decimal(str(tick.bid))
            if current_price >= tp_target:
                self._execute_partial_close(pos, percentage_to_close, f"PTP{rr_target}")
        
        elif pos.type == mt5.ORDER_TYPE_SELL:
            sl_distance = Decimal(str(initial_sl - initial_entry))
            if sl_distance <= 0: return
            tp_target = Decimal(str(initial_entry)) - (sl_distance * rr_target)
            current_price = Decimal(str(tick.ask))
            if current_price <= tp_target:
                self._execute_partial_close(pos, percentage_to_close, f"PTP{rr_target}")

    def _execute_partial_close(self, position, percentage: Decimal, new_comment_flag: str):
        try:
            context = self._executor._trade_context.get(position.ticket)
            if not context: logging.error(f"PTP: Contexte introuvable pour exécution #{position.ticket}."); return
            
            initial_volume = Decimal(str(context.get('initial_volume', position.volume)))
            remaining_volume = Decimal(str(context.get('remaining_volume', position.volume)))
            
            volume_to_close = initial_volume * percentage
            step = Decimal(str(self.volume_step))
            
            if step <= 0: logging.error(f"PTP: Volume step invalide pour {self.symbol}"); return
            
            volume_to_close = (volume_to_close / step).to_integral_value(rounding=ROUND_DOWN) * step
            
            # Ajuster si on essaie de fermer plus qu'il n'en reste
            volume_to_close = min(volume_to_close, remaining_volume)

            vol_min_d = Decimal(str(self.volume_min))
            volume_remaining_after = remaining_volume - volume_to_close
            
            # Gérer la poussière: si ce qu'on ferme ou ce qui reste est < min
            if volume_to_close < vol_min_d:
                 logging.warning(f"PTP {position.ticket}: Vol à fermer ({volume_to_close}) < Min ({vol_min_d}). Annulé.")
                 return # Ne pas fermer
                 
            if volume_remaining_after < vol_min_d and volume_remaining_after > 0:
                 logging.warning(f"PTP {position.ticket}: Vol restant ({volume_remaining_after}) < Min ({vol_min_d}). Fermeture totale (100%).")
                 volume_to_close = remaining_volume # Fermer tout ce qui reste
                 new_comment_flag += "|FullClose"
                 
            # Re-calculer vol_digits basé sur step
            vol_digits = abs(step.as_tuple().exponent) if step < 1 else 0
            volume_to_close_float = round(float(volume_to_close), vol_digits)
            if volume_to_close_float <= 0:
                 logging.warning(f"PTP {position.ticket}: Volume final à fermer est 0. Annulé.")
                 return

            logging.info(f"PTP {new_comment_flag} atteint {self.symbol} ({position.ticket}). Tentative fermeture {volume_to_close_float} lots.")
            
            if self.executor.close_partial_position(position, volume_to_close_float):
                 # Mettre à jour le contexte seulement si succès
                 context['remaining_volume'] = float(remaining_volume - volume_to_close)
                 # Trouver l'index du RR pour le flag d'état
                 rr_level_index = -1
                 for i, lvl in enumerate(self.ptp_rules):
                      if Decimal(str(lvl.get('rr',0))) == rr_target:
                           rr_level_index = i; break
                 if rr_level_index != -1:
                      context['partial_tp_state'][rr_level_index] = True
                 self.log.info(f"PTP #{position.ticket} succès. Volume restant: {context['remaining_volume']:.{vol_digits}f}")
                 # Déplacer à BE+ si c'est le TP1
                 if rr_level_index == 0 and self.breakeven_rules.get('move_to_be_plus_on_ptp1', False):
                      self._apply_breakeven_on_ptp([position], None, ptp1_rr=rr_target) # Appeler BE+
            
        except Exception as e: logging.error(f"Erreur exécution PTP (Ticket {position.ticket}): {e}", exc_info=True)

    def _apply_breakeven_pips(self, positions: list, tick, trigger_pips: int):
        pips_plus = self.breakeven_rules.get('pips_plus', 1.0)
        for pos in positions:
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
        pips_plus = self.breakeven_rules.get('pips_plus_on_ptp1', 5.0)
        for pos in positions:
            # Appliquer BE si le flag PTP1 est là ET BE pas déjà appliqué
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
        if atr <= 0: return
        try:
            atr_float = float(atr)
            activation_distance = float(activation_multiple) * atr_float
            trailing_distance = float(trailing_multiple) * atr_float
        except Exception: return # Erreur conversion
            
        for pos in positions:
            try:
                current_sl_price = pos.sl
                entry_price = pos.price_open
                
                if pos.type == mt5.ORDER_TYPE_BUY:
                    current_price = tick.bid
                    current_profit = current_price - entry_price
                    if current_profit >= activation_distance:
                        potential_new_sl = current_price - trailing_distance
                        # SL doit être > entrée ET > SL actuel
                        if potential_new_sl > entry_price and potential_new_sl > current_sl_price:
                             new_sl = round(potential_new_sl, self.digits)
                             logging.debug(f"Trailing Stop (BUY) {self.symbol} ({pos.ticket}). SL -> {new_sl}")
                             self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "TS_APPLIED")
                
                elif pos.type == mt5.ORDER_TYPE_SELL:
                    current_price = tick.ask
                    current_profit = entry_price - current_price
                    if current_profit >= activation_distance:
                        potential_new_sl = current_price + trailing_distance
                        # SL doit être < entrée ET (< SL actuel ou SL actuel est 0)
                        if potential_new_sl < entry_price and (current_sl_price == 0 or potential_new_sl < current_sl_price):
                             new_sl = round(potential_new_sl, self.digits)
                             logging.debug(f"Trailing Stop (SELL) {self.symbol} ({pos.ticket}). SL -> {new_sl}")
                             self.executor.modify_position_sl_tp(pos.ticket, new_sl, pos.tp, "TS_APPLIED")
            except Exception as e:
                 logging.error(f"Erreur TSL {pos.ticket}: {e}", exc_info=True)


    def is_daily_loss_limit_reached(self) -> tuple:
        """
        Vérifie si la limite de perte journalière (en % equity) est atteinte.
        Réintroduit pour corriger AttributeError.
        """
        try:
            magic_number = self.config['trading_settings'].get('magic_number', 0)
            
            broker_tz_str = self.config.get('mt5_credentials', {}).get('timezone', 'UTC')
            broker_tz = pytz.timezone(broker_tz_str)
            day_start_str = self.risk_settings.get('daily_limit_reset_time_broker', '00:00')
            day_start_time = dt_time.fromisoformat(day_start_str)
            now_broker_time = datetime.now(broker_tz)
            start_of_today = broker_tz.localize(datetime(now_broker_time.year, now_broker_time.month, now_broker_time.day, day_start_time.hour, day_start_time.minute))
            if now_broker_time < start_of_today:
                start_of_today = start_of_today - timedelta(days=1)
            start_of_today_utc = start_of_today.astimezone(pytz.utc)
            now_utc = datetime.now(pytz.utc)

            deals = self.mt5.history_deals_get(start_of_today_utc, now_utc)
            if deals is None:
                logging.error("Impossible de récupérer l'historique des deals (daily loss check).")
                return False, Decimal('0.0')

            total_profit_today = Decimal('0.0')
            for deal in deals:
                if deal.magic == magic_number and deal.entry == mt5.DEAL_ENTRY_OUT:
                     total_profit_today += Decimal(str(deal.profit)) + Decimal(str(deal.commission)) + Decimal(str(deal.swap))

            floating_pl = self.executor.get_total_floating_pl(magic_number)
            total_current_pl = total_profit_today + Decimal(str(floating_pl))
            
            equity = Decimal(str(self.account_info.equity))
            loss_limit_amount = self.daily_loss_limit_pct * equity
            
            current_loss = -total_current_pl if total_current_pl < 0 else Decimal('0.0')

            if current_loss > 0 and current_loss >= loss_limit_amount: # Comparaison Decimal
                logging.critical(f"LIMITE PERTE JOUR ATTEINTE: Perte {current_loss:.2f} {self.account_currency} >= Limite {loss_limit_amount:.2f}")
                return True, float(current_loss) # Retourne float pour compatibilité main
            
            logging.info(f"Check Perte Jour: {current_loss:.2f} / {loss_limit_amount:.2f} {self.account_currency}")
            return False, float(current_loss) # Retourne float

        except Exception as e:
            logging.error(f"Erreur vérification limite perte jour: {e}", exc_info=True)
            return False, 0.0 # Retourne float


    def check_max_concurrent_risk(self, equity: float) -> bool:
        """ Vérifie si le risque total simultané dépasse la limite. """
        if self.max_concurrent_risk_pct <= 0: return True
        try:
            magic_number = self.config['trading_settings'].get('magic_number', 0)
            open_positions = self.executor.get_open_positions(magic_number)
            current_total_risk_pct = Decimal('0.0')
            
            for pos in open_positions:
                 is_at_be_or_profit = False
                 if (pos.type == mt5.ORDER_TYPE_BUY and pos.sl > pos.price_open) or \
                    (pos.type == mt5.ORDER_TYPE_SELL and pos.sl != 0 and pos.sl < pos.price_open): # Ajout check SL != 0
                     is_at_be_or_profit = True
                 
                 if not is_at_be_or_profit:
                     # Idéalement, on recalcule le risque réel basé sur SL actuel vs prix entrée
                     # Simplification:
                     current_total_risk_pct += self.risk_per_trade_pct

            potential_total_risk = current_total_risk_pct + self.risk_per_trade_pct
            limit_pct = self.max_concurrent_risk_pct
            
            if potential_total_risk > limit_pct:
                 logging.warning(f"Check Risque Concurrent: {potential_total_risk*100:.1f}% > Limite {limit_pct*100:.1f}%. Trade bloqué.")
                 return False
            
            logging.info(f"Check Risque Concurrent: {potential_total_risk*100:.1f}% <= Limite {limit_pct*100:.1f}%.")
            return True

        except Exception as e:
             logging.error(f"Erreur vérification risque concurrent: {e}", exc_info=True)
             return False # Prudence: bloquer
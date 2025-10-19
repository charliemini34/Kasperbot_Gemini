# Fichier: src/risk/risk_manager.py
# Version: 17.0.5 (SMC-TP-Validation)
# Dépendances: MetaTrader5, pandas, numpy, logging, pytz, src.constants
# Description: Valide la position du TP SMC par rapport à l'entrée et corrige l'offset SELL.

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
    Gère le risque avec validation du TP SMC et arrondi de volume correct.
    v17.0.5: Assure que le TP SMC est toujours du côté profitable de l'entrée.
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
        # ... (inchangé) ...
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
            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == magic_number and deal.entry == 1)
            
            loss_limit_amount = (self.account_info.equity * loss_limit_percent) / 100.0
            
            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) a atteint la limite de {loss_limit_percent}%.")
                return True, daily_pnl
                
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur critique dans le calcul de la limite de perte journalière : {e}", exc_info=True)
            return False, 0.0

    def calculate_trade_parameters(self, equity: float, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float, float]:
        # ... (inchangé jusqu'à la vérification du volume final) ...
        try:
            if not isinstance(trade_signal, dict) or 'direction' not in trade_signal:
                self.log.error(f"Signal de trade invalide reçu: {trade_signal}. 'direction' manquante.")
                return 0.0, 0.0, 0.0

            risk_percent = self._config.get('risk_management', {}).get('risk_per_trade', 0.01)
            
            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, ohlc_data, trade_signal)
            
            # Vérification renforcée : SL et TP doivent être valides ET différents du prix
            if ideal_sl <= 0 or ideal_tp <= 0 or abs(price - ideal_sl) < self.symbol_info.point * 2 or abs(price - ideal_tp) < self.symbol_info.point * 2 :
                self.log.error(f"SL/TP invalide ou trop proche du prix. SL: {ideal_sl}, TP: {ideal_tp}, Prix: {price}. Trade annulé.")
                return 0.0, 0.0, 0.0

            final_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)

            vol_min_from_api = self.symbol_info.volume_min
            vol_step_from_api = self.symbol_info.volume_step
            self.log.debug(f"DEBUG VOLUME pour {self._symbol}: Vol Final={final_volume:.4f}, Vol Min API={vol_min_from_api}, Vol Step API={vol_step_from_api}")
            
            if final_volume < vol_min_from_api:
                if final_volume == 0.0 and vol_min_from_api > 0:
                     self.log.warning(f"Le volume calculé ({final_volume:.4f}), après ajustement au step ({vol_step_from_api}), est inférieur au min API ({vol_min_from_api}). Trade annulé.")
                     return 0.0, 0.0, 0.0
                elif final_volume != 0.0 : 
                     self.log.warning(f"Le volume final ({final_volume:.4f}) est inférieur au min API ({vol_min_from_api}). Trade annulé.")
                     return 0.0, 0.0, 0.0

            if final_volume <= 0:
                 self.log.warning(f"Le volume final calculé pour {self._symbol} est zéro. Trade annulé.")
                 return 0.0, 0.0, 0.0

            return final_volume, ideal_sl, ideal_tp

        except Exception as e:
            self.log.error(f"Erreur inattendue lors du calcul des paramètres de trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0
    
    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        # ... (inchangé) ...
        self.log.debug("--- DÉBUT DU CALCUL DE VOLUME ---")
        
        risk_amount_account_currency = equity * risk_percent
        self.log.debug(f"1. Capital: {equity:.2f} | Risque: {risk_percent:.2%} -> Montant Risqué: {risk_amount_account_currency:.2f} {self.account_info.currency}")

        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price < self.point:
            self.log.error("Distance SL quasi nulle.")
            return 0.0
        self.log.debug(f"2. Distance SL: {sl_distance_price:.{self.digits}f}")

        loss_per_lot_profit_currency = sl_distance_price * self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        self.log.debug(f"3. Perte/Lot ({profit_currency}): {loss_per_lot_profit_currency:.2f}")

        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if profit_currency != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(profit_currency, self.account_info.currency)
            if not conversion_rate or conversion_rate <= 0:
                self.log.error(f"Taux conversion invalide {profit_currency}->{self.account_info.currency}.")
                return 0.0
            loss_per_lot_account_currency *= conversion_rate
            self.log.debug(f"4. Conversion @ {conversion_rate:.5f} -> Perte/Lot ({self.account_info.currency}): {loss_per_lot_account_currency:.2f}")
        else:
            self.log.debug("4. Pas de conversion nécessaire.")

        if loss_per_lot_account_currency <= 0:
            self.log.error("Perte par lot nulle ou négative.")
            return 0.0

        raw_volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.debug(f"5. Volume Brut: {raw_volume:.6f} lots")

        volume_step = self.symbol_info.volume_step
        if volume_step <= 0:
             self.log.warning("Volume step invalide (<= 0). Utilisation du volume brut.")
             adjusted_volume = raw_volume 
        else:
             adjusted_volume = math.floor(raw_volume / volume_step) * volume_step
             self.log.debug(f"6. Volume Ajusté au Step ({volume_step}): {adjusted_volume:.6f}")
        
        final_volume = max(0, min(self.symbol_info.volume_max, adjusted_volume))
        
        self.log.debug(f"7. Volume Final (après min/max): {final_volume:.4f} (Min API: {self.symbol_info.volume_min}, Max API: {self.symbol_info.volume_max})")
        self.log.debug("--- FIN DU CALCUL DE VOLUME ---")
        return final_volume


    def _calculate_initial_sl_tp(self, price: float, ohlc_data: pd.DataFrame, trade_signal: dict) -> Tuple[float, float]:
        rm_settings = self._config.get('risk_management', {})
        strategy = rm_settings.get('sl_tp_strategy', 'ATR_MULTIPLE')
        direction = trade_signal['direction']

        # Calcul SL (commun et basé sur ATR)
        atr_settings = rm_settings.get('atr_settings', {}).get('default', {})
        atr = self.calculate_atr(ohlc_data, atr_settings.get('period', 14))
        if atr is None or atr <= 0:
            self.log.error("ATR invalide. Impossible de calculer le SL.")
            return 0.0, 0.0
        
        sl_multiple = atr_settings.get('sl_multiple', 1.5)
        sl_distance = atr * sl_multiple
        sl = price - sl_distance if direction == BUY else price + sl_distance
        
        # Initialisation TP
        tp = 0.0
        tp_calculated = False # Flag pour savoir si on a calculé un TP

        # Calcul TP basé sur Stratégie SMC
        if strategy == "SMC_LIQUIDITY_TARGET":
            target_price = trade_signal.get('target_price')
            if target_price:
                # --- MODIFICATION : Validation de la cible SMC ---
                is_target_valid = False
                if direction == BUY and target_price > price:
                    is_target_valid = True
                elif direction == SELL and target_price < price:
                    is_target_valid = True
                
                if is_target_valid:
                    # Appliquer un offset pour ne pas viser exactement le niveau
                    tp_offset = self.symbol_info.point * 10 # Ex: 1 pip de marge
                    tp = target_price - tp_offset if direction == BUY else target_price + tp_offset # Correction offset SELL
                    self.log.debug(f"Stratégie SMC: Cible liquidité valide ({target_price:.{self.digits}f}), TP ajusté à {tp:.{self.digits}f}")
                    tp_calculated = True
                else:
                    self.log.warning(f"Stratégie SMC: Cible liquidité ({target_price:.{self.digits}f}) invalide par rapport au prix ({price:.{self.digits}f}). Passage en mode ATR.")
            else:
                self.log.warning("Stratégie SMC choisie mais aucune cible de liquidité trouvée. Passage en mode ATR.")

        # Calcul TP basé sur Stratégie ATR (si SMC échoue ou si ATR est choisi)
        if not tp_calculated: # S'exécute si strategy == "ATR_MULTIPLE" OU si la cible SMC était invalide/manquante
            tp_multiple = atr_settings.get('tp_multiple', 3.0)
            tp_distance = atr * tp_multiple
            tp = price + tp_distance if direction == BUY else price - tp_distance
            self.log.debug(f"Stratégie ATR utilisée: TP calculé à {tp:.{self.digits}f}")
            tp_calculated = True # Marquer comme calculé

        # Vérification finale si aucun TP n'a pu être calculé
        if not tp_calculated or tp <= 0:
             self.log.error("Impossible de calculer un TP valide.")
             return 0.0, 0.0 # Retourne 0 pour SL et TP pour annuler le trade

        # Validation Ratio R/R
        if sl > 0 and abs(tp - price) < abs(sl - price):
            self.log.warning(f"Le TP ({tp:.{self.digits}f}) est plus proche que le SL ({sl:.{self.digits}f}) (Ratio < 1).")

        return round(sl, self.digits), round(tp, self.digits)
        
    def get_conversion_rate(self, from_currency: str, to_currency: str) -> Optional[float]:
        # ... (inchangé) ...
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
        # ... (inchangé) ...
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period:
            return None
        
        required_cols = ['high', 'low', 'close']
        if not all(col in ohlc_data.columns for col in required_cols):
             self.log.error(f"Colonnes manquantes pour calculer l'ATR: {required_cols}")
             return None
        df_copy = ohlc_data.copy()
        df_copy[required_cols] = df_copy[required_cols].apply(pd.to_numeric, errors='coerce')
        df_copy.dropna(subset=required_cols, inplace=True)
        if len(df_copy) < period:
             return None

        high_low = df_copy['high'] - df_copy['low']
        high_close = np.abs(df_copy['high'] - df_copy['close'].shift())
        low_close = np.abs(df_copy['low'] - df_copy['close'].shift())
        
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        
        atr_series = true_range.ewm(span=period, adjust=False).mean()
        
        last_atr = atr_series.iloc[-1]
        if pd.isna(last_atr):
            self.log.warning(f"Calcul ATR a retourné NaN pour la dernière valeur.")
            return None
        
        return last_atr

    def manage_open_positions(self, positions: list, current_tick, ohlc_data: pd.DataFrame):
        # ... (inchangé) ...
        if not positions or not current_tick or ohlc_data is None or ohlc_data.empty: return
        
        risk_settings = self._config.get('risk_management', {})
        if risk_settings.get('breakeven', {}).get('enabled', False):
            self._apply_breakeven(positions, current_tick, risk_settings.get('breakeven', {}))
            
        if risk_settings.get('trailing_stop_atr', {}).get('enabled', False):
            self._apply_trailing_stop_atr(positions, current_tick, ohlc_data, risk_settings)

    def _apply_breakeven(self, positions: list, tick, be_cfg: dict):
        # ... (inchangé) ...
        trigger_pips = be_cfg.get('trigger_pips', 150)
        pips_plus = be_cfg.get('pips_plus', 10)
        
        for pos in positions:
            pnl_pips = 0.0
            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_pips = (tick.bid - pos.price_open) / self.point
                if pos.sl != 0 and pos.sl < pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open + (pips_plus * self.point)
                    self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_pips = (pos.price_open - tick.ask) / self.point
                if pos.sl != 0 and pos.sl > pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open - (pips_plus * self.point)
                    self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
    
    def _apply_trailing_stop_atr(self, positions: list, tick, ohlc_data: pd.DataFrame, risk_cfg: dict):
        # ... (inchangé) ...
        ts_cfg = risk_cfg.get('trailing_stop_atr', {})
        atr_cfg = risk_cfg.get('atr_settings', {}).get('default', {})
        
        period = atr_cfg.get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0: return

        activation_multiple = ts_cfg.get('activation_multiple', 2.0)
        trailing_multiple = ts_cfg.get('trailing_multiple', 1.8)
        
        for pos in positions:
            new_sl = pos.sl
            current_sl = pos.sl 

            if pos.type == mt5.ORDER_TYPE_BUY:
                if (tick.bid - pos.price_open) >= (atr * activation_multiple):
                    potential_new_sl = tick.bid - (atr * trailing_multiple)
                    if potential_new_sl > current_sl and potential_new_sl > pos.price_open:
                        new_sl = potential_new_sl
            elif pos.type == mt5.ORDER_TYPE_SELL:
                if (pos.price_open - tick.ask) >= (atr * activation_multiple):
                    potential_new_sl = tick.ask + (atr * trailing_multiple)
                    if (current_sl == 0 or potential_new_sl < current_sl) and potential_new_sl < pos.price_open:
                        new_sl = potential_new_sl
                        
            new_sl_rounded = round(new_sl, self.digits)
            if new_sl_rounded != round(current_sl, self.digits):
                self.log.info(f"TRAILING STOP: Mise à jour du SL pour #{pos.ticket} à {new_sl_rounded:.{self.digits}f}")
                self._executor.modify_position(pos.ticket, new_sl_rounded, pos.tp)
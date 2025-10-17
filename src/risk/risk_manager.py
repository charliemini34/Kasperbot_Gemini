# Fichier: src/risk/risk_manager.py
# Version: 14.0.0 (Guardian+ Enhanced)
# Dépendances: MetaTrader5, pandas, numpy, logging
# Description: Moteur de risque sécurisé avec des validations robustes et une journalisation détaillée.

import MetaTrader5 as mt5
import logging
import math
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from src.constants import BUY, SELL

class RiskManager:
    """
    Gère le risque des trades avec des garde-fous essentiels et des calculs précis.
    """
    def __init__(self, config: dict, executor, symbol: str):
        self.log = logging.getLogger(self.__class__.__name__)
        self._config = config
        self._executor = executor
        self._symbol = symbol
        
        self.symbol_info = self._executor._mt5.symbol_info(self._symbol)
        self.account_info = self._executor._mt5.account_info()
        
        if not self.symbol_info or not self.account_info:
            self.log.critical(f"Impossible d'obtenir les informations pour le symbole {self._symbol} ou pour le compte.")
            raise ValueError("Informations de compte ou de symbole MT5 manquantes.")
            
        self.point = self.symbol_info.point
        self.digits = self.symbol_info.digits

    def is_daily_loss_limit_reached(self) -> (bool, float):
        """Vérifie si la limite de perte journalière est atteinte."""
        loss_limit_percent = self._config.get('risk_management', {}).get('daily_loss_limit_percent', 2.0)
        if loss_limit_percent <= 0: return False, 0.0
        
        try:
            broker_tz = pytz.timezone("EET") # Heure du courtier (Europe de l'Est)
            now_in_broker_tz = datetime.now(broker_tz)
            today_start_broker_tz = now_in_broker_tz.replace(hour=0, minute=0, second=0, microsecond=0)
            
            history_deals = self._executor._mt5.history_deals_get(today_start_broker_tz, datetime.now())
            if history_deals is None: 
                self.log.warning("Impossible de récupérer l'historique des transactions pour la limite de perte journalière.")
                return False, 0.0

            daily_pnl = sum(deal.profit for deal in history_deals if deal.magic == self._config['trading_settings']['magic_number'])
            
            loss_limit_amount = (self.account_info.equity * loss_limit_percent) / 100.0
            
            if daily_pnl < 0 and abs(daily_pnl) >= loss_limit_amount:
                self.log.critical(f"ARRÊT D'URGENCE: Perte journalière ({daily_pnl:.2f}) a atteint la limite de {loss_limit_percent}%.")
                return True, daily_pnl
                
            return False, daily_pnl
        except Exception as e:
            self.log.error(f"Erreur critique dans le calcul de la limite de perte journalière : {e}", exc_info=True)
            return False, 0.0 # Par précaution, on retourne False pour ne pas bloquer le bot sur une erreur

    def calculate_trade_parameters(self, equity: float, price: float, direction: str, ohlc_data: pd.DataFrame):
        """Calcule le volume, le stop-loss et le take-profit pour un trade."""
        try:
            risk_settings = self._config.get('risk_management', {})
            risk_percent = risk_settings.get('risk_per_trade', 0.01)
            
            ideal_sl, ideal_tp = self._calculate_initial_sl_tp(price, direction, ohlc_data)
            
            # SANITY CHECK: Assure que le SL n'est pas à zéro ou invalide
            if ideal_sl == 0 or abs(price - ideal_sl) < self.symbol_info.point:
                self.log.error(f"La distance du Stop Loss est invalide ({ideal_sl}). Le trade est annulé.")
                return 0.0, 0.0, 0.0

            ideal_volume = self._calculate_volume(equity, risk_percent, price, ideal_sl)

            # Ajustement si le volume est inférieur au minimum autorisé par le courtier
            if ideal_volume < self.symbol_info.volume_min:
                self.log.warning(f"Le volume idéal ({ideal_volume:.4f}) est inférieur au min ({self.symbol_info.volume_min}). Trade annulé pour préserver le capital.")
                return 0.0, 0.0, 0.0
                
            return ideal_volume, ideal_sl, ideal_tp

        except Exception as e:
            self.log.error(f"Erreur lors du calcul des paramètres de trade : {e}", exc_info=True)
            return 0.0, 0.0, 0.0

    def _calculate_volume(self, equity: float, risk_percent: float, entry_price: float, sl_price: float) -> float:
        """Calcule la taille de la position en fonction du risque, avec conversion de devise."""
        self.log.debug("--- DÉBUT DU CALCUL DE VOLUME SÉCURISÉ ---")
        
        # 1. Montant à risquer
        risk_amount_account_currency = equity * risk_percent
        self.log.debug(f"1. Capital: {equity:.2f} | Risque: {risk_percent:.2%} -> Montant à risquer: {risk_amount_account_currency:.2f} {self.account_info.currency}")

        # 2. Distance du Stop Loss
        sl_distance_price = abs(entry_price - sl_price)
        if sl_distance_price <= 1e-5: # Garde-fou pour éviter division par zéro
            self.log.error("Distance du SL quasi nulle. Annulation du calcul de volume.")
            return 0.0
        self.log.debug(f"2. Distance SL: {sl_distance_price:.{self.digits}f} (en prix de l'actif)")

        # 3. Perte par lot
        loss_per_lot_profit_currency = sl_distance_price * self.symbol_info.trade_contract_size
        profit_currency = self.symbol_info.currency_profit
        self.log.debug(f"3. Perte/Lot en devise de profit ({profit_currency}): {loss_per_lot_profit_currency:.2f}")

        # 4. Conversion de devise (si nécessaire)
        loss_per_lot_account_currency = loss_per_lot_profit_currency
        if profit_currency != self.account_info.currency:
            conversion_rate = self.get_conversion_rate(profit_currency, self.account_info.currency)
            if not conversion_rate or conversion_rate <= 0:
                self.log.error(f"Impossible d'obtenir un taux de conversion valide pour {profit_currency}->{self.account_info.currency}. Annulation.")
                return 0.0
            loss_per_lot_account_currency /= conversion_rate
            self.log.debug(f"4. Conversion: {profit_currency}/{self.account_info.currency} @ {conversion_rate:.5f} -> Perte/Lot en devise du compte: {loss_per_lot_account_currency:.2f}")
        else:
            self.log.debug("4. Pas de conversion de devise nécessaire.")

        if loss_per_lot_account_currency <= 0:
            self.log.error("La perte par lot calculée est nulle ou négative. Annulation.")
            return 0.0

        # 5. Calcul du volume
        volume = risk_amount_account_currency / loss_per_lot_account_currency
        self.log.debug(f"5. Volume brut: {risk_amount_account_currency:.2f} / {loss_per_lot_account_currency:.2f} = {volume:.4f} lots")

        # 6. Ajustement au pas de volume et aux limites du courtier
        volume_step = self.symbol_info.volume_step
        volume = math.floor(volume / volume_step) * volume_step
        final_volume = round(max(self.symbol_info.volume_min, min(self.symbol_info.volume_max, volume)), 2)
        
        self.log.debug(f"6. Volume final ajusté: {final_volume:.2f} (Min: {self.symbol_info.volume_min}, Max: {self.symbol_info.volume_max}, Step: {volume_step})")
        self.log.debug("--- FIN DU CALCUL DE VOLUME ---")
        return final_volume

    def _calculate_initial_sl_tp(self, price: float, direction: str, ohlc_data: pd.DataFrame):
        rm_settings = self._config.get('risk_management', {})
        strategy = rm_settings.get('sl_tp_strategy', 'ATR_MULTIPLE')
        
        if strategy == "ATR_MULTIPLE":
            symbol_settings = rm_settings.get('atr_settings', {}).get(self._symbol, rm_settings.get('atr_settings', {}).get('default', {}))
            period = symbol_settings.get('period', 14)
            atr = self.calculate_atr(ohlc_data, period)
            sl_multiple = symbol_settings.get('sl_multiple', 1.5)
            tp_multiple = symbol_settings.get('tp_multiple', 3.0)
            
            if atr is None or atr <= 0:
                self.log.error("ATR invalide. Impossible de calculer SL/TP.")
                return 0, 0
            
            if direction == BUY:
                sl = price - (atr * sl_multiple)
                tp = price + (atr * tp_multiple)
            else: # SELL
                sl = price + (atr * sl_multiple)
                tp = price - (atr * tp_multiple)
                
            return round(sl, self.digits), round(tp, self.digits)
            
        self.log.error(f"La stratégie SL/TP '{strategy}' n'est pas reconnue.")
        return 0, 0

    def get_conversion_rate(self, from_currency: str, to_currency: str) -> float | None:
        """Trouve le taux de change pour convertir une devise en une autre."""
        if from_currency == to_currency: return 1.0
        
        # Essai direct (ex: EURUSD pour convertir USD en EUR)
        pair1 = f"{from_currency}{to_currency}"
        info1 = self._executor._mt5.symbol_info_tick(pair1)
        if info1 and info1.ask > 0:
            self.log.debug(f"Taux de change direct trouvé pour {pair1}: {1.0 / info1.ask}")
            return 1.0 / info1.ask

        # Essai inversé (ex: EURUSD pour convertir EUR en USD)
        pair2 = f"{to_currency}{from_currency}"
        info2 = self._executor._mt5.symbol_info_tick(pair2)
        if info2 and info2.bid > 0:
            self.log.debug(f"Taux de change inverse trouvé pour {pair2}: {info2.bid}")
            return info2.bid

        self.log.error(f"Impossible de trouver une paire de conversion pour {from_currency} -> {to_currency}")
        return None
    
    def calculate_atr(self, ohlc_data: pd.DataFrame, period: int) -> float | None:
        """Calcule l'Average True Range (ATR)."""
        if ohlc_data is None or ohlc_data.empty or len(ohlc_data) < period:
            return None
        
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
        
        trigger_pips = cfg.get('trigger_pips', 150)
        pips_plus = cfg.get('pips_plus', 10)
        
        for pos in positions:
            if pos.type == mt5.ORDER_TYPE_BUY:
                pnl_pips = (tick.bid - pos.price_open) / self.point
                if pos.sl < pos.price_open and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open + (pips_plus * self.point)
                    self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
            elif pos.type == mt5.ORDER_TYPE_SELL:
                pnl_pips = (pos.price_open - tick.ask) / self.point
                if (pos.sl == 0 or pos.sl > pos.price_open) and pnl_pips >= trigger_pips:
                    breakeven_sl = pos.price_open - (pips_plus * self.point)
                    self.log.info(f"BREAK-EVEN déclenché pour le ticket #{pos.ticket}. Nouveau SL: {breakeven_sl:.{self.digits}f}")
                    self._executor.modify_position(pos.ticket, breakeven_sl, pos.tp)
    
    def _apply_trailing_stop_atr(self, positions, tick, ohlc_data):
        cfg = self._config.get('risk_management', {}).get('trailing_stop_atr', {})
        period = self._config.get('risk_management', {}).get('atr_settings', {}).get('default', {}).get('period', 14)
        atr = self.calculate_atr(ohlc_data, period)
        if atr is None or atr <= 0: return

        activation_multiple = cfg.get('activation_multiple', 2.0)
        trailing_multiple = cfg.get('trailing_multiple', 1.8)
        
        for pos in positions:
            new_sl = pos.sl
            if pos.type == mt5.ORDER_TYPE_BUY:
                if (tick.bid - pos.price_open) >= (atr * activation_multiple):
                    potential_new_sl = tick.bid - (atr * trailing_multiple)
                    if potential_new_sl > pos.sl:
                        new_sl = potential_new_sl
            elif pos.type == mt5.ORDER_TYPE_SELL:
                if (pos.price_open - tick.ask) >= (atr * activation_multiple):
                    potential_new_sl = tick.ask + (atr * trailing_multiple)
                    if new_sl == 0 or potential_new_sl < new_sl:
                        new_sl = potential_new_sl
                        
            if new_sl != pos.sl:
                self.log.info(f"TRAILING STOP: Mise à jour du SL pour #{pos.ticket} à {new_sl:.{self.digits}f}")
                self._executor.modify_position(pos.ticket, new_sl, pos.tp)